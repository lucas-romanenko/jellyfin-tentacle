"""The original-release rule.

For a MusicBrainz release group (an album, across all its editions):

1. Take its OFFICIAL releases that have a date. The earliest year is the
   original year.
2. Among that year's releases, ignore digital-only ones when there are
   physical ones: a "Digital Media" release dated 1974 is a reissue with the
   wrong date, and it once tied a real LP.
3. The most common total track count is the original tracklist length. A tie
   for most common is ambiguous and goes to review: never guess.
4. The most common disc count among those releases is the original disc
   count. (Not the fewest: MusicBrainz lists many cassettes as one medium, and
   "fewest" turned genuine double albums into one-disc ones.)
5. Pin the Lidarr release with exactly that track count, preferring an
   official release, no special-edition words, the same disc count, a
   preferred format, then a preferred country.

A release pinned to a different count one track away from the original,
whose files match it, may be a hidden-track edition; it goes to review rather
than being trimmed. So does an album with no dated official release, or one
Lidarr has no release of the right length for.

Everything here is pure: MusicBrainz and Lidarr data in, a verdict out.
"""
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Optional, Union

RIGHT = "right"
REPIN = "repin"
REPIN_TRIM = "repin_trim"
REPIN_DOWNLOAD = "repin_download"
REVIEW = "review"
CATEGORIES = (RIGHT, REPIN, REPIN_TRIM, REPIN_DOWNLOAD, REVIEW)

# Lidarr's and MusicBrainz's names for the same countries; preferences are ISO codes.
_COUNTRY_NAMES = {
    "US": "united states", "GB": "united kingdom", "CA": "canada", "XW": "[worldwide]",
    "XE": "europe", "AU": "australia", "DE": "germany", "FR": "france", "JP": "japan",
    "NL": "netherlands", "IT": "italy", "ES": "spain", "SE": "sweden", "BR": "brazil",
}


@dataclass
class Prefs:
    countries: list = field(default_factory=lambda: ["US", "GB", "CA", "XW"])
    formats: list = field(default_factory=lambda: ["CD", "Digital Media"])
    edition_words: list = field(default_factory=lambda: [
        "deluxe", "expanded", "anniversary", "bonus", "live", "mono", "stereo",
        "box", "collector", "legacy", "super"])

    @classmethod
    def from_settings(cls, db) -> "Prefs":
        from models.database import get_setting
        from services.music.settings import DEFAULTS

        def items(key, lower=False):
            raw = get_setting(db, key, DEFAULTS[key])
            out = [p.strip() for p in (raw or "").split(",") if p.strip()]
            return [p.lower() for p in out] if lower else out
        return cls(countries=[c.upper() for c in items("music_preferred_countries")],
                   formats=items("music_preferred_formats"),
                   edition_words=items("music_edition_words", lower=True))


@dataclass
class Original:
    year: str
    tracks: int
    discs: int
    counts: dict           # track count -> how many first-year releases had it
    near: list             # other first-year counts one track away (possible hidden-track editions)
    releases: list         # the first-year MusicBrainz releases with the original tracklist
    pool: list = field(default_factory=list)  # every first-year release the count was taken from
    digital_only_year: bool = False  # the first year had only digital releases


@dataclass
class Ambiguous:
    reason: str            # tie | no_dated_release
    message: str
    options: list = field(default_factory=list)


@dataclass
class Verdict:
    category: str
    message: str = ""
    reason: str = ""                      # for review: tie | hidden_track | no_dated_release | no_matching_release
    original: Optional[dict] = None       # {year, tracks, discs}
    target: Optional[dict] = None         # the Lidarr release to pin
    pinned: Optional[dict] = None         # the Lidarr release pinned now
    have: int = 0                         # track files on disk
    locked: bool = False                  # "any release OK" is off
    options: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ── MusicBrainz releases ─────────────────────────────────────────────────

def track_count(release: dict) -> int:
    return sum((m.get("track-count") or 0) for m in release.get("media") or [])


def disc_count(release: dict) -> int:
    return len(release.get("media") or [])


def media_formats(release: dict) -> list:
    return [m.get("format") or "" for m in release.get("media") or []]


def is_digital_only(release: dict) -> bool:
    formats = media_formats(release)
    return bool(formats) and all(f == "Digital Media" for f in formats)


def _year(release: dict) -> str:
    date = (release.get("date") or "")[:4]
    return date if date.isdigit() else ""


def _option(releases: list, tracks: int) -> dict:
    with_count = [r for r in releases if track_count(r) == tracks]
    discs = Counter(disc_count(r) for r in with_count)
    examples = []
    for r in with_count[:3]:
        fmt = "+".join(dict.fromkeys(f for f in media_formats(r) if f)) or "unknown format"
        examples.append(f"{r.get('title') or '?'} ({fmt}{', ' + r['country'] if r.get('country') else ''})")
    return {"tracks": tracks, "releases": len(with_count),
            "discs": _most_common(discs) if discs else 1, "examples": examples}


def _most_common(counter: Counter) -> int:
    # Most common; a tie goes to the smaller value.
    return max(counter.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def find_original(mb_releases: list) -> Union[Original, Ambiguous]:
    dated = [r for r in mb_releases or []
             if (r.get("status") or "Official").lower() == "official" and _year(r) and track_count(r) > 0]
    if not dated:
        return Ambiguous("no_dated_release",
                         "MusicBrainz has no dated official release of this album, so its original "
                         "tracklist is unknown.")
    year = min(_year(r) for r in dated)
    first = [r for r in dated if _year(r) == year]
    physical = [r for r in first if not is_digital_only(r)]
    pool = physical or first
    ranked = Counter(track_count(r) for r in pool).most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        tied = sorted(c for c, k in ranked if k == ranked[0][1])
        return Ambiguous("tie",
                         f"In {year}, releases with {' and '.join(str(c) for c in tied)} tracks are equally "
                         f"common, so the original tracklist isn't clear.",
                         [_option(pool, c) for c in tied])
    tracks = ranked[0][0]
    with_tracks = [r for r in pool if track_count(r) == tracks]
    counts = dict(ranked)
    return Original(year=year, tracks=tracks, discs=_most_common(Counter(disc_count(r) for r in with_tracks)),
                    counts=counts, near=sorted(c for c in counts if abs(c - tracks) == 1),
                    releases=with_tracks, pool=pool, digital_only_year=not physical)


# ── Preferences ──────────────────────────────────────────────────────────

_WORD = re.compile(r"\w+", re.UNICODE)


def edition_marked(text: str, words: list, album_title: str = "") -> bool:
    """A special-edition word in `text` that isn't part of the album's own title
    ("Live" doesn't mark every release of "Live at Leeds")."""
    own = set(_WORD.findall((album_title or "").lower()))
    found = set(_WORD.findall((text or "").lower())) - own
    return any(w in found for w in words)


def _base_formats(fmt: str) -> list:
    # Lidarr: "2xCD", '2x12" Vinyl', "CD, DVD-Video"
    return [re.sub(r"^\d+x", "", part.strip()) for part in (fmt or "").split(",") if part.strip()]


def format_rank(formats: list, preferred: list) -> int:
    low = [p.lower() for p in preferred]
    ranks = [low.index(f.lower()) for f in formats if f.lower() in low]
    return min(ranks) if ranks else len(low)


def country_rank(countries: list, preferred: list) -> int:
    ranks = []
    for c in countries or []:
        c_low = (c or "").strip().lower()
        for i, code in enumerate(preferred):
            if c_low == code.lower() or c_low == _COUNTRY_NAMES.get(code.upper(), "\0") or \
                    (code.upper() == "XW" and c_low == "worldwide"):
                ranks.append(i)
    return min(ranks) if ranks else len(preferred)


def _lidarr_key(r: dict, original: Original, prefs: Prefs, mb_index: dict, album_title: str):
    mb = mb_index.get(r.get("foreignReleaseId")) or {}
    text = f"{r.get('title') or ''} {r.get('disambiguation') or mb.get('disambiguation') or ''}"
    formats = media_formats(mb) if mb else _base_formats(r.get("format"))
    countries = [mb["country"]] if mb.get("country") else (r.get("country") or [])
    return (
        (r.get("status") or "Official").lower() != "official",
        edition_marked(text, prefs.edition_words, album_title),
        (r.get("mediumCount") or 1) != original.discs,
        format_rank(formats, prefs.formats),
        country_rank(countries, prefs.countries),
        r.get("foreignReleaseId") or "",
    )


def choose_lidarr_release(original: Original, releases: list, prefs: Prefs,
                          mb_index: Optional[dict] = None, album_title: str = "") -> Optional[dict]:
    """The Lidarr release to pin: exactly the original track count, best by preference."""
    fits = [r for r in releases or [] if r.get("trackCount") == original.tracks]
    if not fits:
        return None
    return min(fits, key=lambda r: _lidarr_key(r, original, prefs, mb_index or {}, album_title))


def representative_release(original: Original, prefs: Prefs, album_title: str = "") -> dict:
    """The MusicBrainz release that stands for the original (the album page's tracklist)."""
    def key(r):
        return (edition_marked(f"{r.get('title') or ''} {r.get('disambiguation') or ''}",
                               prefs.edition_words, album_title),
                disc_count(r) != original.discs,
                format_rank(media_formats(r), prefs.formats),
                country_rank([r.get("country")] if r.get("country") else [], prefs.countries),
                r.get("date") or "9999", r.get("id") or "")
    return min(original.releases, key=key)


# ── Verdicts ─────────────────────────────────────────────────────────────

def summarize(release: Optional[dict]) -> Optional[dict]:
    if not release:
        return None
    return {"id": release.get("foreignReleaseId"), "lidarr_id": release.get("id"),
            "title": release.get("title"), "disambiguation": release.get("disambiguation") or "",
            "format": release.get("format") or "", "discs": release.get("mediumCount") or 1,
            "tracks": release.get("trackCount") or 0, "country": release.get("country") or []}


def _review_options(options: list, releases: list, prefs: Prefs, mb_index: dict, album_title: str) -> list:
    """Attach, to each tracklist option, the Lidarr release that would be pinned for it."""
    out = []
    for opt in options:
        stand_in = Original(year="", tracks=opt["tracks"], discs=opt.get("discs") or 1, counts={}, near=[],
                            releases=[])
        target = choose_lidarr_release(stand_in, releases, prefs, mb_index, album_title)
        out.append(dict(opt, target=summarize(target)))
    return out


def lidarr_options(releases: list, prefs: Prefs, mb_index: dict, album_title: str) -> list:
    """When MusicBrainz can't say what the original is: Lidarr's releases, one option
    per track count, so the user still has something concrete to pick."""
    out = []
    for tracks in sorted({r.get("trackCount") for r in releases or [] if r.get("trackCount")}):
        with_count = [r for r in releases if r.get("trackCount") == tracks]
        discs = _most_common(Counter(r.get("mediumCount") or 1 for r in with_count))
        stand_in = Original(year="", tracks=tracks, discs=discs, counts={}, near=[], releases=[])
        target = choose_lidarr_release(stand_in, with_count, prefs, mb_index, album_title)
        out.append({"tracks": tracks, "releases": len(with_count), "discs": discs,
                    "examples": [f"{r.get('title') or '?'} ({r.get('format') or 'unknown format'})" for r in with_count[:3]],
                    "target": summarize(target)})
    return out


def evaluate(album: dict, mb_releases: list, prefs: Prefs) -> Verdict:
    """What to do with one Lidarr album (its resource, with releases and statistics)."""
    releases = album.get("releases") or []
    pinned = next((r for r in releases if r.get("monitored")), None)
    have = int((album.get("statistics") or {}).get("trackFileCount") or 0)
    base = dict(pinned=summarize(pinned), have=have, locked=not album.get("anyReleaseOk", True))
    title = album.get("title") or ""
    mb_index = {r.get("id"): r for r in mb_releases or []}

    found = find_original(mb_releases)
    if isinstance(found, Ambiguous):
        options = (_review_options(found.options, releases, prefs, mb_index, title) if found.options
                   else lidarr_options(releases, prefs, mb_index, title))
        return Verdict(REVIEW, found.message, reason=found.reason, options=options, **base)
    orig = found
    original = {"year": orig.year, "tracks": orig.tracks, "discs": orig.discs}

    near = next((c for c in orig.near if pinned and pinned.get("trackCount") == c and have == c), None)
    if near is not None:
        options = [_option(orig.pool, orig.tracks), _option(orig.pool, near)]
        return Verdict(REVIEW, f"In {orig.year} most releases had {orig.tracks} tracks, but "
                               f"{orig.counts.get(near, 0)} had {near}, like yours: possibly a hidden or extra "
                               f"track. Choose which tracklist to keep.",
                       reason="hidden_track", original=original,
                       options=_review_options(options, releases, prefs, mb_index, title), **base)

    target = choose_lidarr_release(orig, releases, prefs, mb_index, title)
    if pinned and pinned.get("trackCount") == orig.tracks and (
            (pinned.get("mediumCount") or 1) == orig.discs or target is None
            or (target.get("mediumCount") or 1) != orig.discs):
        return Verdict(RIGHT, "Pinned to a release with the original tracklist.", original=original,
                       target=summarize(pinned), **base)
    if target is None:
        by_count = sorted({r.get("trackCount") for r in releases if r.get("trackCount")})
        return Verdict(REVIEW, f"The original has {orig.tracks} tracks ({orig.year}), but none of the "
                               f"releases Lidarr knows has {orig.tracks}"
                               f"{' (it has ' + ', '.join(map(str, by_count)) + ')' if by_count else ''}.",
                       reason="no_matching_release", original=original,
                       options=lidarr_options(releases, prefs, mb_index, title), **base)

    if have == 0 or have < orig.tracks:
        category, what = REPIN_DOWNLOAD, ("then download it" if have == 0
                                          else f"then download the {orig.tracks - have} missing tracks")
    elif have > orig.tracks:
        category, what = REPIN_TRIM, f"then remove the {have - orig.tracks} extra tracks"
    else:
        category, what = REPIN, "your files already fit it"
    return Verdict(category, f"Re-pin to the {orig.tracks}-track original ({orig.year}); {what}.",
                   original=original, target=summarize(target), **base)


def choose_for_request(album: dict, mb_releases: list, prefs: Prefs,
                       choice: Optional[dict] = None) -> Union[dict, Ambiguous]:
    """The release to pin for a new request: the original, or the user's explicit choice.

    choice: {"release_id": <MusicBrainz release id>} or {"tracks": N}.
    """
    releases = album.get("releases") or []
    title = album.get("title") or ""
    mb_index = {r.get("id"): r for r in mb_releases or []}
    if choice and choice.get("release_id"):
        match = next((r for r in releases if r.get("foreignReleaseId") == choice["release_id"]), None)
        if match:
            return match
        return Ambiguous("no_matching_release", "Lidarr doesn't know the release you picked.")
    if choice and choice.get("tracks"):
        tracks = int(choice["tracks"])
        with_count = [r for r in mb_releases or [] if track_count(r) == tracks]
        discs = _most_common(Counter(disc_count(r) for r in with_count)) if with_count else 1
        stand_in = Original(year="", tracks=tracks, discs=discs, counts={}, near=[], releases=with_count)
        target = choose_lidarr_release(stand_in, releases, prefs, mb_index, title)
        return target or Ambiguous("no_matching_release", f"Lidarr has no release with {tracks} tracks.")
    found = find_original(mb_releases)
    if isinstance(found, Ambiguous):
        return Ambiguous(found.reason, found.message,
                         _review_options(found.options, releases, prefs, mb_index, title) if found.options
                         else lidarr_options(releases, prefs, mb_index, title))
    target = choose_lidarr_release(found, releases, prefs, mb_index, title)
    if target is None:
        return Ambiguous("no_matching_release",
                         f"The original has {found.tracks} tracks ({found.year}), but Lidarr has no release "
                         f"with {found.tracks}.", lidarr_options(releases, prefs, mb_index, title))
    return target
