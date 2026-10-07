"""Mislabelled provider streams: block them, remove them, and spot them.

An IPTV provider's catalogue is just names and stream numbers, and Tentacle can
only trust the name. When the provider mislabels a stream — found live: stream
188327, listed as "The Decline of Western Civilization" (1981), actually plays
the 2020 Netflix film "The Decline" — Tentacle files it under the wrong film:
the right poster, "In Library", and the wrong movie on Play. Worse, it hides
the real title: a Radarr request for it looked already satisfied.

- block_and_remove_movie(): the admin "Wrong movie" action. Blocks the stream
  so no sync re-imports it, then removes Tentacle's copy everywhere.
- check_runtime_mismatches(): Jellyfin probes a .strm on first play; a probed
  length far from TMDB's runtime flags the title for the admin.
"""
import logging
import re
import threading
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from models.database import (
    BlockedStream, MatchSuspect, Movie, get_setting, log_deletion,
)

logger = logging.getLogger(__name__)

# Xtream movie URL: {server}/movie/{user}/{pass}/{stream_id}.{ext}
_XTREAM_MOVIE = re.compile(r"/movie/[^/]+/[^/]+/(\d+)\.[A-Za-z0-9]+$")

# A probed length this far from TMDB's is a different film, not a different cut:
# both absolute (trailers/credits vary by a few minutes) and relative.
MISMATCH_MINUTES = 12
MISMATCH_FRACTION = 0.15


def stream_key_for_url(url: str) -> Optional[str]:
    """The key a stream is blocked under: its Xtream id, else the whole URL."""
    url = (url or "").strip()
    if not url:
        return None
    from services import vod_tokens
    via_tentacle = vod_tokens.stream_id_in_url(url)
    if via_tentacle:
        return str(via_tentacle[1])
    m = _XTREAM_MOVIE.search(url)
    return m.group(1) if m else url


def stream_key_from_strm(strm_path: str) -> Optional[str]:
    try:
        return stream_key_for_url(Path(strm_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None


def blocked_keys(db: Session, provider_id: int, media_type: str = "movie") -> set:
    return {k for (k,) in db.query(BlockedStream.stream_key).filter(
        BlockedStream.provider_id == provider_id,
        BlockedStream.media_type == media_type,
    ).all()}


def is_blocked(keys: set, stream_id, url: str = "") -> bool:
    """Whether a catalogue entry is blocked — by id, or by URL for M3U.

    A block is stored under stream_key_for_url() of the .strm, so the entry's
    URL is compared the same way: an M3U export of an Xtream panel lists VOD
    as /movie/<user>/<pass>/<id>.<ext>, which is stored as the bare <id>
    while the M3U client's own stream_id is a hash of the URL."""
    if not keys:
        return False
    if stream_id is not None and str(stream_id) in keys:
        return True
    return bool(url) and (url in keys or stream_key_for_url(url) in keys)


class WrongMatchError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def block_and_remove_movie(db: Session, tmdb_id: int, user_name: str = None,
                           reason: str = "wrong movie") -> dict:
    """Block the provider stream behind a VOD movie and remove the movie.

    Order matters: Tentacle's record goes before the Jellyfin item. Deleting
    the Jellyfin item fires the plugin's delete hook, whose clean-up also drops
    the title's download request — and a request for the REAL film (Radarr
    searching for it) is exactly what must survive. With the record already
    gone, the hook finds nothing to clean.
    """
    from services.media_files import delete_movie_files

    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    if not (row.source or "").startswith("provider_") or not row.provider_id:
        raise WrongMatchError(400, "Only IPTV (VOD) titles can be reported — this one is a download")
    key = stream_key_from_strm(row.strm_path)
    if not key:
        raise WrongMatchError(409, "Could not read which provider stream this title plays, so it "
                                   "cannot be blocked (its .strm file is missing)")

    title, provider_id, jf_item_id, strm_path = row.title, row.provider_id, row.jellyfin_item_id, row.strm_path
    exists = db.query(BlockedStream).filter(
        BlockedStream.provider_id == provider_id,
        BlockedStream.media_type == "movie",
        BlockedStream.stream_key == key,
    ).first()
    if exists is None:
        db.add(BlockedStream(provider_id=provider_id, media_type="movie", stream_key=key,
                             tmdb_id=tmdb_id, title=title, reason=reason, blocked_by=user_name))

    files = delete_movie_files(row.strm_path)
    if Path(row.strm_path).exists():
        # Still on disk (permissions, a read-only mount): Jellyfin would keep
        # playing the wrong film while the row says it is gone.
        db.rollback()
        raise WrongMatchError(500, "Couldn't delete this copy's .strm file, so nothing was changed. "
                                   "Check that Tentacle can write to the VOD folder.")
    db.delete(row)
    db.query(MatchSuspect).filter(MatchSuspect.tmdb_id == tmdb_id,
                                  MatchSuspect.media_type == "movie").delete()
    db.commit()

    shown_key = key if key.isdigit() else "its stream URL"
    log_deletion(db, kind="wrong-match", name=title, media_type="movie", reason="manual",
                 user_name=user_name,
                 detail=f"Provider {provider_id} stream {shown_key} blocked ({reason}); "
                        f"{files} VOD file(s) removed")
    logger.info(f"[WrongMatch] '{title}' (tmdb:{tmdb_id}): blocked provider {provider_id} "
                f"stream {shown_key}, removed {files} file(s) — by {user_name}")

    jf_deleted = _delete_from_jellyfin(db, tmdb_id, jf_item_id, strm_path)
    _refresh_caches()
    return {"ok": True, "title": title, "blocked": shown_key, "files_removed": files,
            "jellyfin_deleted": jf_deleted,
            "message": f"Removed the wrong copy of {title} and blocked that stream"}


def _strm_tail(strm_path: Optional[str]) -> Optional[str]:
    """'<folder>/<file>.strm' -- what the .strm's path ends with in Jellyfin too
    (Jellyfin mounts the VOD folder elsewhere, so the prefix differs, and the
    mount's own name differs too: /media/vod/movies here, e.g. /vod-movies in
    Jellyfin -- so only two components can be compared)."""
    parts = Path(strm_path).parts if strm_path else ()
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def _is_item_for_strm(item: Optional[dict], tail: Optional[str]) -> bool:
    path = ((item or {}).get("Path") or "").replace("\\", "/")
    return bool(tail) and path.endswith("/" + tail)


def jellyfin_item_for_strm(jf, tmdb_id: int, strm_path: Optional[str],
                           jf_item_id: Optional[str] = None) -> Optional[dict]:
    """The Jellyfin item that plays THIS .strm -- never just "the first movie
    with this TMDB id". The same film is often in Jellyfin twice: a Radarr
    download next to the IPTV copy (or the owner's own file), and deleting
    the wrong one through Jellyfin deletes that download from disk. A stored
    id is only trusted when it is this file (Discover backfills the id from a
    TMDB lookup, which can be the download's)."""
    tail = _strm_tail(strm_path)
    if not tail:
        return None
    if jf_item_id:
        try:
            item = jf.get_item_by_id(jf_item_id)
        except Exception:
            item = None
        if _is_item_for_strm(item, tail):
            return item
    tmdb_str, start = str(tmdb_id), 0
    while True:
        data = jf._get("/Items", params={
            "IncludeItemTypes": "Movie", "Recursive": "true", "Fields": "ProviderIds,Path",
            "EnableImages": "false", "EnableUserData": "false", "StartIndex": start, "Limit": 10000})
        if not isinstance(data, dict):
            return None
        page = data.get("Items") or []
        for item in page:
            if (item.get("ProviderIds") or {}).get("Tmdb") == tmdb_str and _is_item_for_strm(item, tail):
                return item
        start += len(page)
        if not page or start >= (data.get("TotalRecordCount") or 0):
            return None


def _delete_from_jellyfin(db: Session, tmdb_id: int, jf_item_id: Optional[str],
                          strm_path: Optional[str] = None, cleanup_playlists: bool = True) -> bool:
    """Let Jellyfin drop this copy's item. Never DELETE /Items: Jellyfin deletes
    what IT sees -- for a .strm that is the primary of a multi-version item,
    the item's whole folder, download included; otherwise every sidecar that
    starts with the title -- and its view of the folder can hold files
    Tentacle's doesn't (mergerfs/unionfs pools, separate mounts). Tentacle has
    removed its .strm/.nfo, so a library scan drops the item and leaves every
    other file alone. The item's playlist entries are removed by its own id
    (never "the first movie with this TMDB id", which can be a download).
    Returns False: nothing is deleted through Jellyfin."""
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return False
    try:
        from services.jellyfin import JellyfinService
        jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
        item = jellyfin_item_for_strm(jf, tmdb_id, strm_path, jf_item_id)
        try:
            jf.trigger_library_scan(None)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"[WrongMatch] Jellyfin lookup/scan for tmdb:{tmdb_id} failed: {e}")
        return False
    logger.info(f"[WrongMatch] Removed this copy's files; a library scan drops its Jellyfin item "
                f"(tmdb:{tmdb_id}, item {item['Id'] if item else 'not found'})")
    if item is not None and cleanup_playlists:
        from routers.library import _cleanup_playlists_all_users
        threading.Thread(target=_cleanup_playlists_all_users,
                         args=(tmdb_id, "movie", item["Id"]), daemon=True).start()
    return False


def _refresh_caches() -> None:
    """In-library state changed: Discover badges and Activity must re-read it."""
    for mod, fn in (("routers.activity", "invalidate_wanted_cache"),
                    ("routers.discover", "bust_arr_ids_cache"),
                    ("routers.discover", "bust_known_ids_cache")):
        try:
            getattr(__import__(mod, fromlist=[fn]), fn)()
        except Exception:
            pass


# ── Detection ────────────────────────────────────────────────────────────────

def is_mismatch(expected_minutes, actual_minutes) -> bool:
    if not expected_minutes or not actual_minutes or expected_minutes <= 0 or actual_minutes <= 0:
        return False
    diff = abs(actual_minutes - expected_minutes)
    return diff >= MISMATCH_MINUTES and diff / expected_minutes >= MISMATCH_FRACTION


def check_runtime_mismatches(db: Session) -> dict:
    """Flag VOD movies whose probed length is far from their TMDB runtime.

    One Jellyfin listing; only titles Jellyfin has probed (played at least once)
    carry a real length, so the set grows as the library gets watched. A
    dismissed flag stays dismissed; flags for titles that are gone or now match
    are cleared.
    """
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return {"checked": 0, "flagged": 0}
    from services.jellyfin import JellyfinService
    jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
    items = jf.query_movies_with_media_sources()
    if items is None:
        return {"checked": 0, "flagged": 0, "error": "Jellyfin listing failed"}

    expected = {m.tmdb_id: (m.runtime, m.title) for m in db.query(
        Movie.tmdb_id, Movie.runtime, Movie.title).filter(Movie.source.like("provider_%")).all()}
    existing = {s.tmdb_id: s for s in db.query(MatchSuspect).filter(MatchSuspect.media_type == "movie").all()}

    checked, flagged, still = 0, 0, set()
    for item in items:
        try:
            tmdb_id = int((item.get("ProviderIds") or {}).get("Tmdb") or 0)
        except (TypeError, ValueError):
            continue
        if tmdb_id not in expected:
            continue
        # Only the IPTV copy itself. The same film is often in Jellyfin as a
        # real file too (a Radarr download, possibly another cut); its length
        # says nothing about what the provider's stream plays.
        path = (item.get("Path") or "").lower()
        if path and not path.endswith(".strm"):
            continue
        src = strm_source(item)
        ticks = ((src or {}).get("RunTimeTicks")) or 0
        actual = round(ticks / 600_000_000) if ticks else 0
        runtime, title = expected[tmdb_id]
        if not actual or not runtime:
            continue
        checked += 1
        if not is_mismatch(runtime, actual):
            continue
        still.add(tmdb_id)
        s = existing.get(tmdb_id)
        if s is None:
            db.add(MatchSuspect(tmdb_id=tmdb_id, media_type="movie", title=title,
                                expected_minutes=runtime, actual_minutes=actual,
                                jellyfin_item_id=item.get("Id")))
            flagged += 1
            logger.warning(f"[WrongMatch] '{title}' plays {actual} min but TMDB says {runtime} — "
                           f"probably a different film under the provider's label")
        else:
            s.expected_minutes, s.actual_minutes = runtime, actual
    # Flags whose title is gone, or that no longer mismatch (unless dismissed —
    # the admin's call stands).
    for tmdb_id, s in existing.items():
        if tmdb_id not in still and not s.dismissed:
            db.delete(s)
    db.commit()
    logger.info(f"[WrongMatch] Runtime check: {checked} probed VOD movie(s) compared, {flagged} newly flagged")
    return {"checked": checked, "flagged": flagged}


# ── Fixing the match instead of removing it ──────────────────────────────────
# Usually the stream IS a real film, just not the one on the label. Tentacle
# can find it the way a person would: the stream's real length (Jellyfin's
# probe) plus the label's own words — a mislabel tends to be a similarly named
# film ("The Decline of Western Civilization" → "The Decline", 83 min = 83 min).

RUNTIME_CLOSE = 3        # minutes: "this is the length of that film"
MAX_SUGGESTIONS = 8


def _tmdb(db: Session):
    from services.tmdb import TMDBService, get_tmdb_token
    token = get_tmdb_token(db)
    if not token:
        raise WrongMatchError(503, "TMDB is not available")
    return TMDBService(token, get_setting(db, "data_dir", "/data"))


TMDB_DOWN = "TMDB can't be reached right now, so films can't be looked up. Try again in a minute."


def _tmdb_call(tmdb, fn, *args):
    """One TMDB lookup: (result, failed). An unreachable TMDB raises
    TMDBConnectionError and a 429/5xx answers None -- neither means "no such
    film", and neither may surface as an unhandled 500."""
    from services.exceptions import TMDBConnectionError
    try:
        tmdb._tl.failed = False
    except AttributeError:
        pass
    try:
        result = fn(*args)
    except TMDBConnectionError:
        return None, True
    failed = bool(getattr(tmdb, "_lookup_failed", lambda: False)())
    return result, failed and not result


def _jf(db: Session):
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return None
    from services.jellyfin import JellyfinService
    return JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))


# Jellyfin labels audio tracks with ISO 639-2 codes, TMDB films with ISO 639-1.
_LANG_3_TO_1 = {
    "eng": "en", "fre": "fr", "fra": "fr", "spa": "es", "ger": "de", "deu": "de",
    "ita": "it", "por": "pt", "dut": "nl", "nld": "nl", "swe": "sv", "nor": "no",
    "nob": "no", "dan": "da", "fin": "fi", "pol": "pl", "rus": "ru", "ukr": "uk",
    "tur": "tr", "gre": "el", "ell": "el", "heb": "he", "ara": "ar", "per": "fa",
    "fas": "fa", "hin": "hi", "tam": "ta", "tel": "te", "mal": "ml", "ben": "bn",
    "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "cmn": "zh", "yue": "cn",
    "tha": "th", "vie": "vi", "ind": "id", "may": "ms", "msa": "ms", "fil": "tl",
    "tgl": "tl", "cze": "cs", "ces": "cs", "slo": "sk", "slk": "sk", "hun": "hu",
    "rum": "ro", "ron": "ro", "bul": "bg", "srp": "sr", "hrv": "hr", "ice": "is",
    "isl": "is", "cat": "ca", "baq": "eu", "eus": "eu", "glg": "gl", "est": "et",
    "lav": "lv", "lit": "lt", "slv": "sl",
}
LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian",
    "pt": "Portuguese", "nl": "Dutch", "sv": "Swedish", "no": "Norwegian", "da": "Danish",
    "fi": "Finnish", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian", "tr": "Turkish",
    "el": "Greek", "he": "Hebrew", "ar": "Arabic", "fa": "Persian", "hi": "Hindi",
    "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "bn": "Bengali", "ja": "Japanese",
    "ko": "Korean", "zh": "Chinese", "cn": "Cantonese", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "ms": "Malay", "tl": "Tagalog", "cs": "Czech", "sk": "Slovak",
    "hu": "Hungarian", "ro": "Romanian", "bg": "Bulgarian", "sr": "Serbian", "hr": "Croatian",
    "is": "Icelandic", "ca": "Catalan", "eu": "Basque", "gl": "Galician", "et": "Estonian",
    "lv": "Latvian", "lt": "Lithuanian", "sl": "Slovenian",
}


def language_code(code: Optional[str]) -> Optional[str]:
    """A track or film language as ISO 639-1, or None when unknown/undetermined."""
    c = (code or "").strip().lower()
    if not c or c in ("und", "unk", "mul", "zxx", "mis", "xx", "qaa"):
        return None
    if len(c) == 2:
        return c
    return _LANG_3_TO_1.get(c[:3])


def strm_source(item: dict) -> Optional[dict]:
    """The media source that is the .strm itself. With a download of the same
    film in the same folder, Jellyfin groups the two as versions of one item
    and lists the widest first -- MediaSources[0] is then the download."""
    sources = (item or {}).get("MediaSources") or []
    item_id = (item or {}).get("Id")
    for src in sources:
        if (src.get("Path") or "").lower().endswith(".strm"):
            return src
    for src in sources:
        if item_id and src.get("Id") == item_id:
            return src
    return None if len(sources) > 1 else (sources[0] if sources else None)


def probe_info(db: Session, row: Movie) -> dict:
    """What Jellyfin's probe learned about the stream, if it has been played:
    its real length in minutes and the languages of its audio tracks."""
    empty = {"minutes": None, "audio_languages": []}
    jf = _jf(db)
    if jf is None:
        return empty
    try:
        found = jellyfin_item_for_strm(jf, row.tmdb_id, row.strm_path, row.jellyfin_item_id)
        if not found:
            return empty
        item = jf.get_item_by_id(found["Id"]) or {}
        src = strm_source(item) or {}
        ticks = src.get("RunTimeTicks") or 0
        langs = []
        for s in (src.get("MediaStreams") or []):
            if s.get("Type") == "Audio":
                code = language_code(s.get("Language"))
                if code and code not in langs:
                    langs.append(code)
        return {"minutes": round(ticks / 600_000_000) or None, "audio_languages": langs}
    except Exception as e:
        logger.debug(f"[WrongMatch] No probe for tmdb:{row.tmdb_id}: {e}")
        return empty


def probed_minutes(db: Session, row: Movie) -> Optional[int]:
    """The stream's real length from Jellyfin's probe, if it has been played."""
    return probe_info(db, row)["minutes"]


def _title_queries(title: str) -> list:
    """The label, then shorter and shorter prefixes of it (at least one real word)."""
    words = (title or "").split()
    out = []
    for n in range(len(words), 0, -1):
        q = " ".join(words[:n]).strip(" :-,")
        if q and q.lower() not in ("the", "a", "an") and q not in out:
            out.append(q)
        if len(out) >= 6:
            break
    return out


def suggest_matches(db: Session, tmdb_id: int, query: Optional[str] = None) -> dict:
    """Films this VOD movie might really be, best first."""
    from services.tmdb import TMDBService  # noqa: F401 (typing)
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    tmdb = _tmdb(db)
    probe = probe_info(db, row)
    actual = probe["minutes"]
    audio = probe["audio_languages"]
    queries = [query.strip()] if query and query.strip() else _title_queries(row.title)

    seen, found, failures = {tmdb_id}, [], 0
    for q in queries:
        data, failed = _tmdb_call(tmdb, tmdb._request, "search/movie", {"query": q})
        failures += failed
        data = data or {}
        for r in (data.get("results") or [])[:10]:
            if r.get("id") in seen:
                continue
            seen.add(r["id"])
            found.append(r)
        if len(found) >= 24:
            break

    if not found and failures:
        raise WrongMatchError(503, TMDB_DOWN)

    in_lib = {t for (t,) in db.query(Movie.tmdb_id).filter(Movie.tmdb_id.in_([r["id"] for r in found])).all()} if found else set()
    label = (row.title or "").lower()
    candidates = []
    for r in found[:24]:
        details = _tmdb_call(tmdb, tmdb.get_movie_details, r["id"])[0] or {}
        runtime = details.get("runtime") or None
        title = r.get("title") or details.get("title") or ""
        close = bool(actual and runtime and abs(runtime - actual) <= RUNTIME_CLOSE)
        lang = language_code(r.get("original_language") or details.get("original_language"))
        candidates.append({
            "tmdb_id": r["id"], "title": title,
            "year": (r.get("release_date") or "")[:4] or None,
            "runtime": runtime, "poster_path": r.get("poster_path"),
            "overview": (r.get("overview") or "")[:240],
            "runtime_matches": close, "in_library": r["id"] in in_lib,
            "original_language": lang,
            "language_name": LANGUAGE_NAMES.get(lang, lang.upper()) if lang else None,
            # Only a clue when the stream has a single audio language: a
            # multi-track stream (original + dubs) says little about the film.
            "language_matches": bool(lang and len(audio) == 1 and audio[0] == lang),
            "_sim": tmdb._similarity(label, title), "_pop": r.get("popularity") or 0,
        })

    def rank(c):
        if actual and c["runtime"]:
            gap = abs(c["runtime"] - actual)
        else:
            gap = 999
        # Length first (when known): same length, then how close; the audio
        # language only breaks a tie in length (a single audio track is often
        # a dub, #199); then how much of the label it shares, then fame.
        return (0 if c["runtime_matches"] else 1, gap if actual else 0,
                0 if c["language_matches"] else 1, -c["_sim"], -c["_pop"])

    candidates.sort(key=rank)
    for c in candidates:
        c.pop("_sim"), c.pop("_pop")
    return {
        "current": {"tmdb_id": row.tmdb_id, "title": row.title, "year": row.year, "runtime": row.runtime},
        "actual_minutes": actual,
        "audio_languages": [{"code": c, "name": LANGUAGE_NAMES.get(c, c.upper())} for c in audio],
        "searched": queries,
        "candidates": candidates[:MAX_SUGGESTIONS],
    }


def _radarr_folder_ids(db: Session, name: str) -> set:
    """TMDB ids of Radarr downloads in a folder of this NAME: in the merged
    Radarr/VOD layout Radarr sees /data/movies/X and Tentacle /media/vod/movies/X."""
    return {tid for tid, path in db.query(Movie.tmdb_id, Movie.radarr_path)
            .filter(Movie.radarr_path.isnot(None)).all() if Path(path).parent.name == name}


def _folder_is_this_copys(db: Session, row: Movie) -> bool:
    """True when the copy's folder holds nothing but this copy: no download
    (any importable video), no other .strm or NFO, no sub-folder, no other
    row's file. Subtitles and artwork are the stream's own. Then "Fix it" can
    rewrite the NFO where it is, and Jellyfin keeps the item (#294)."""
    from services.media_files import MEDIA_SUFFIXES
    strm = Path(row.strm_path)
    folder = strm.parent
    own = {strm.name, strm.with_suffix(".nfo").name}
    if row.nfo_path:
        own.add(Path(row.nfo_path).name)
    try:
        for f in folder.iterdir():
            if f.name in own or _is_own_movie_nfo(f, row.tmdb_id):
                continue
            if f.is_dir() or f.suffix.lower() in MEDIA_SUFFIXES | {".strm", ".nfo"}:
                return False
    except OSError:
        return False
    if _radarr_folder_ids(db, folder.name):
        return False
    return db.query(Movie).filter(Movie.id != row.id,
                                  Movie.strm_path.like(str(folder).replace("%", "_") + "/%")).first() is None


def _is_own_movie_nfo(f: Path, tmdb_id: int) -> bool:
    """A movie.nfo naming this copy's film is the copy's own: Jellyfin's NFO
    saver (library option "Metadata savers: Nfo") writes movie.nfo beside the
    .strm on the item's first metadata download or tag update."""
    if f.name.lower() != "movie.nfo":
        return False
    from services.sync import _nfo_tmdb_ids
    return _nfo_tmdb_ids(f.parent, [f.name]) == {tmdb_id}


def _folder_taken(db: Session, row: Movie, folder: Path, new_tmdb_id: int) -> bool:
    """Another title owns `folder` (#293, the sync's #155/#185 rule): another
    row's .strm, a Radarr download of another film in a folder of that name,
    an NFO naming another TMDB id, or a video that isn't this copy."""
    from services.media_files import MEDIA_SUFFIXES
    from services.sync import _nfo_tmdb_ids
    if db.query(Movie).filter(Movie.id != row.id,
                              Movie.strm_path.like(str(folder).replace("%", "_") + "/%")).first() is not None:
        return True
    if _radarr_folder_ids(db, folder.name) - {new_tmdb_id}:
        return True
    if not folder.is_dir():
        return False
    try:
        entries = [f for f in folder.iterdir() if str(f) != row.strm_path]
    except OSError:
        return True  # cannot look inside: do not write into it
    own_nfo = str(Path(row.strm_path).with_suffix(".nfo"))
    if _nfo_tmdb_ids(folder, [f.name for f in entries
                              if f.suffix.lower() == ".nfo" and str(f) != own_nfo]) - {new_tmdb_id}:
        return True
    return any(f.suffix.lower() in MEDIA_SUFFIXES | {".strm"} for f in entries)


# ── "Fix it" in place (#294): the item keeps its id, Jellyfin gets the new identity ──
# The .strm stays, so Jellyfin keeps the item and every user's data on it. Its
# identity is set with an ItemUpdate before Tentacle commits anything: a scan
# or a Default refresh re-reads the rewritten NFO but keeps every field the NFO
# lacks (the old film's parental rating, cast, IMDb id, list tags), and a
# ReplaceAllMetadata refresh skips the NFO when the library's NFO saver is on
# (Jellyfin then shows the old film again). When Jellyfin can't be told,
# nothing changes: the NFOs are put back and the admin is asked to try again.

_rematch_lock = threading.Lock()   # one "Fix it" at a time (two tabs, two admins)

JELLYFIN_DOWN = ("Jellyfin didn't answer, so nothing was changed: the fix has to reach Jellyfin too, "
                 "or it would keep showing the old film's details. Try again in a minute.")
_DATEADDED = re.compile(rb"^[ \t]*<dateadded>(.*?)</dateadded>[ \t]*\r?\n?", re.MULTILINE)


def _nfo_snapshot(folder: Path) -> Optional[dict]:
    """Every NFO in the copy's folder, byte for byte: with the NFO saver on,
    Jellyfin writes movie.nfo there itself when the item is updated. None when
    one can't be read: an empty snapshot would make a restore delete them all."""
    try:
        return {f: f.read_bytes() for f in folder.iterdir() if f.suffix.lower() == ".nfo" and f.is_file()}
    except OSError as e:
        logger.error(f"[WrongMatch] Could not read the NFOs in {folder}: {e}")
        return None


def _restore_nfos(folder: Path, snapshot: dict) -> None:
    """Put every NFO back as it was; an NFO that wasn't there is removed."""
    try:
        for f in folder.iterdir():
            if f.suffix.lower() == ".nfo" and f not in snapshot:
                f.unlink()
    except OSError as e:
        logger.error(f"[WrongMatch] Could not remove a new NFO in {folder}: {e}")
    for f, data in snapshot.items():
        try:
            if not f.exists() or f.read_bytes() != data:
                f.write_bytes(data)
        except OSError as e:
            logger.error(f"[WrongMatch] Could not restore {f}: {e}")


def _write_nfo_in_place(nfo: Path, new: dict, tags: list, old: Optional[bytes]) -> bool:
    """Write the right film's NFO over the copy's, keeping its <dateadded>
    (Jellyfin reads it as the item's DateCreated: a new one moves the film to
    the top of everyone's "Recently added"); none before, none now. Written to
    a temporary file and renamed, so a failure never leaves half a file."""
    from services.nfo import write_movie_nfo
    tmp = nfo.with_name(f".{nfo.name}.tmp")
    try:
        if not write_movie_nfo(tmp, new, tags):
            return False
        data = tmp.read_bytes()
        m = _DATEADDED.search(old or b"")
        data = _DATEADDED.sub(lambda _m: m.group(0) if m else b"", data, count=1)
        tmp.write_bytes(data)
        tmp.replace(nfo)
        return True
    except OSError as e:
        logger.error(f"[WrongMatch] Could not write {nfo}: {e}")
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _jellyfin_for_fix(db: Session):
    """JellyfinService with a user for the user-scoped GET (every field an
    ItemUpdate must echo), or None when Jellyfin isn't configured."""
    jf = _jf(db)
    if jf is not None and not jf.user_id:
        from models.database import TentacleUser
        admin = db.query(TentacleUser).filter(TentacleUser.is_admin.is_(True)).first()
        jf.user_id = admin.jellyfin_user_id if admin else ""
    return jf


def _strm_item(jf, strm_path: str, jf_item_id: Optional[str]) -> Optional[dict]:
    """This .strm's Jellyfin item, by its path alone (its TMDB id may be
    anything by now), full. None: Jellyfin answered and has no such item.
    Raises JellyfinUnavailable when it didn't answer."""
    tail = _strm_tail(strm_path)
    if jf_item_id:
        item = jf.get_item_strict(jf_item_id)
        if _is_item_for_strm(item, tail):
            return item
    for it in jf.movie_paths_strict():
        if _is_item_for_strm(it, tail):
            return jf.get_item_strict(it["Id"])
    return None


def _identity_changes(db: Session, item: dict, new: dict, tags: list) -> dict:
    """The ItemUpdate fields that make `item` the right film. A field the admin
    locked in Jellyfin (LockedFields) is left as it is; CustomRating and tags
    that aren't Tentacle's are the admin's, about the stream, and stay."""
    from services.tagger import merge_owned_tags, tentacle_owned_tags
    locked = set(item.get("LockedFields") or [])
    title = new.get("title") or item.get("Name") or ""
    year = str(new.get("year") or "")
    ids = {"Tmdb": str(new["tmdb_id"])}
    if new.get("imdb_id"):
        ids["Imdb"] = new["imdb_id"]
    changes = {"ProviderIds": ids, "ProductionYear": int(year) if year.isdigit() else None,
               "PremiereDate": None, "CommunityRating": new.get("rating") or None,
               "CriticRating": None, "ForcedSortName": None,
               "Taglines": [new["tagline"]] if new.get("tagline") else []}
    by_field = {
        "Name": {"Name": title, "OriginalTitle": title},
        "Overview": {"Overview": new.get("overview") or ""},
        "Genres": {"Genres": new.get("genres") or []},
        "Tags": {"Tags": merge_owned_tags(item.get("Tags"), tags, tentacle_owned_tags(db))},
        "Studios": {"Studios": []},
        "Cast": {"People": []},
        "OfficialRating": {"OfficialRating": ""},
        "ProductionLocations": {"ProductionLocations": []},
    }
    for field, values in by_field.items():
        if field not in locked:
            changes.update(values)
    return changes


def _set_identity(jf, item: dict, changes: dict) -> None:
    """ItemUpdate the item to the right film. A lost reply is told from a
    failure by reading the item back. Raises JellyfinUnavailable."""
    from services.jellyfin import JellyfinUnavailable, _item_update_payload
    try:
        jf.update_item(item["Id"], _item_update_payload(item, **changes))
        return
    except JellyfinUnavailable as e:
        err = e
    try:
        now = jf.get_item_strict(item["Id"])
    except JellyfinUnavailable:
        raise err
    if ((now or {}).get("ProviderIds") or {}).get("Tmdb") != changes["ProviderIds"]["Tmdb"]:
        raise err


def _undo_identity(jf, item: dict) -> None:
    """Put the item's old identity back (Tentacle couldn't save the fix)."""
    from services.jellyfin import _item_update_payload
    try:
        jf.update_item(item["Id"], _item_update_payload(item))
    except Exception as e:
        logger.error(f"[WrongMatch] Could not put Jellyfin item {item['Id']} back after a failed fix: {e}")
        try:
            from models.database import SessionLocal, log_activity
            s = SessionLocal()
            try:
                log_activity(s, "fix_it_undo_failed",
                             f"'Fix it' failed and Jellyfin item '{item.get('Name')}' could not be put back: "
                             f"it may show the other film until you refresh its metadata in Jellyfin.")
            finally:
                s.close()
        except Exception:
            pass


def _rematch_in_place(db: Session, row: Movie, key: str, new: dict, tags: list, tmdb_id: int,
                      new_tmdb_id: int, user_name: Optional[str], chown_path) -> dict:
    from services.jellyfin import JellyfinUnavailable
    strm = Path(row.strm_path)
    nfo = Path(row.nfo_path) if row.nfo_path else strm.with_suffix(".nfo")
    old_title = row.title
    jf = _jellyfin_for_fix(db)
    item = None
    if jf is not None:
        try:
            item = _strm_item(jf, row.strm_path, row.jellyfin_item_id)
        except JellyfinUnavailable as e:
            logger.warning(f"[WrongMatch] Fix it for tmdb:{tmdb_id}: Jellyfin didn't answer ({e}); nothing changed")
            raise WrongMatchError(502, JELLYFIN_DOWN)
        if item is not None and item.get("LockData"):
            raise WrongMatchError(409, "This film's metadata is locked in Jellyfin, so it can't be changed to "
                                       "another film. Unlock it in Jellyfin (Edit metadata), then try again. "
                                       "Nothing was changed.")
    snapshot = _nfo_snapshot(strm.parent)
    if snapshot is None:
        raise WrongMatchError(500, "Couldn't read the film's NFO files, so nothing was changed. "
                                   "Check that Tentacle can read the VOD folder.")
    if not _write_nfo_in_place(nfo, new, tags, snapshot.get(nfo)):
        _restore_nfos(strm.parent, snapshot)
        raise WrongMatchError(500, "Couldn't write the film's NFO file, so nothing was changed. "
                                   "Check that Tentacle can write to the VOD folder.")
    chown_path(nfo)
    refreshed = None
    if item is not None:
        try:
            _set_identity(jf, item, _identity_changes(db, item, new, tags))
        except JellyfinUnavailable as e:
            _restore_nfos(strm.parent, snapshot)
            logger.warning(f"[WrongMatch] Fix it for tmdb:{tmdb_id}: Jellyfin didn't take the update ({e}); "
                           f"nothing changed")
            raise WrongMatchError(502, JELLYFIN_DOWN)
    try:
        _apply_rematch(db, row, key, new, tags, tmdb_id, new_tmdb_id, user_name,
                       strm, nfo, item["Id"] if item else row.jellyfin_item_id)
    except Exception as e:
        db.rollback()
        if item is not None:
            _undo_identity(jf, item)   # before the NFOs: with the NFO saver on it writes movie.nfo
        _restore_nfos(strm.parent, snapshot)
        logger.error(f"[WrongMatch] Fix it for tmdb:{tmdb_id} could not be saved: {e}")
        raise WrongMatchError(500, "Couldn't save the fix, so nothing was changed. Try again.")
    # Only now that the fix is saved: a refresh or scan is queued in Jellyfin and
    # downloads the new film's images, which no ItemUpdate can take back.
    if item is not None:
        # Fill what was just cleared (poster, rating, cast) from the NFO and the
        # new TMDB id; ReplaceAllMetadata stays off, so nothing else is touched.
        refreshed = jf.refresh_item_identity(item["Id"])
    elif jf is not None:
        try:
            jf.trigger_library_scan(None)
        except Exception:
            pass
    _audit_rematch(db, key, old_title, row, tmdb_id, new_tmdb_id, user_name)
    if refreshed is False:
        from models.database import log_activity
        log_activity(db, "fix_it_refresh_failed",
                     f"'{row.title}' was fixed, but Jellyfin didn't refresh its poster and details; "
                     f"refresh its metadata in Jellyfin if they stay empty.")
    _refresh_caches()
    if item is None and jf is not None:
        tail = "Jellyfin shows it after its next library scan."
    elif refreshed is False:
        tail = "Jellyfin has the new title; its poster and details come with its next metadata refresh."
    else:
        tail = "Everyone's watched state and playlists are kept."
    return {"ok": True, "title": row.title, "year": row.year, "tmdb_id": new_tmdb_id, "in_place": True,
            "message": f"Fixed: this is {row.title} ({row.year}). {tail}"}


def _apply_rematch(db: Session, row: Movie, key: str, new: dict, tags: list, tmdb_id: int, new_tmdb_id: int,
                   user_name: Optional[str], new_strm: Path, new_nfo: Path, jf_item_id: Optional[str]) -> None:
    """Tentacle's side of a fix: the row becomes the right film, pinned by a
    MatchOverride; committed."""
    from models.database import Duplicate, MatchOverride
    old_strm = row.strm_path
    # A duplicate record pairing the old film with THIS stream no longer holds.
    for dup in db.query(Duplicate).filter(Duplicate.tmdb_id == tmdb_id, Duplicate.media_type == "movie").all():
        if any((src or {}).get("path") == old_strm for src in (dup.sources or [])):
            db.delete(dup)
    row.tmdb_id = new_tmdb_id
    row.title = new.get("title") or row.title
    row.year = new.get("year")
    row.overview = new.get("overview")
    row.runtime = new.get("runtime")
    row.rating = new.get("rating")
    row.genres = new.get("genres") or []
    row.poster_path = new.get("poster_path")
    row.backdrop_path = new.get("backdrop_path")
    row.strm_path, row.nfo_path = str(new_strm), str(new_nfo)
    row.tags = tags
    row.jellyfin_item_id = jf_item_id
    ov = db.query(MatchOverride).filter(MatchOverride.provider_id == row.provider_id,
                                        MatchOverride.media_type == "movie",
                                        MatchOverride.stream_key == key).first()
    if ov is None:
        db.add(MatchOverride(provider_id=row.provider_id, media_type="movie", stream_key=key,
                             tmdb_id=new_tmdb_id, previous_tmdb_id=tmdb_id, title=row.title, set_by=user_name))
    else:
        ov.tmdb_id, ov.title, ov.set_by = new_tmdb_id, row.title, user_name
    db.query(MatchSuspect).filter(MatchSuspect.tmdb_id == tmdb_id,
                                  MatchSuspect.media_type == "movie").delete()
    db.commit()


def _audit_rematch(db: Session, key: str, old_title: str, row: Movie, tmdb_id: int, new_tmdb_id: int,
                   user_name: Optional[str]) -> None:
    log_deletion(db, kind="rematch", name=old_title, media_type="movie", reason="manual", user_name=user_name,
                 detail=f"Stream {key if key.isdigit() else '(URL)'} re-matched: '{old_title}' (tmdb:{tmdb_id}) "
                        f"→ '{row.title}' (tmdb:{new_tmdb_id})")
    logger.info(f"[WrongMatch] Re-matched stream {key if key.isdigit() else '(URL)'}: '{old_title}' → "
                f"'{row.title}' ({row.year}) by {user_name}")


def rematch_movie(db: Session, tmdb_id: int, new_tmdb_id: int, user_name: str = None) -> dict:
    """This VOD stream is really `new_tmdb_id`: the copy becomes that film.

    Same stream, new identity: the NFO is rewritten with the right film's
    metadata, tags tied to the old film (lists, rules) are recomputed, and a
    MatchOverride keeps the sync from undoing it. The .strm stays where it is
    when the folder is only this copy's, so Jellyfin keeps the item and every
    user's watched state, resume point, favourite and playlist entries
    (#294); the folder keeps the label's name. A copy sharing its folder
    (a download there) moves to the right film's folder, or to
    "<Title (Year)> [tmdbid-N]" when another title owns that one (#293).
    If the right film is already in the library, this copy is simply a
    duplicate — it is removed and its stream blocked instead.
    """
    with _rematch_lock:
        return _rematch_movie(db, tmdb_id, new_tmdb_id, user_name)


def _rematch_movie(db: Session, tmdb_id: int, new_tmdb_id: int, user_name: Optional[str]) -> dict:
    from services.media_files import delete_movie_files
    from services.nfo import vod_folder_name, write_movie_nfo
    from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id

    if new_tmdb_id == tmdb_id:
        raise WrongMatchError(400, "That is the film it is already matched to")
    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    if not (row.source or "").startswith("provider_") or not row.provider_id:
        raise WrongMatchError(400, "Only IPTV (VOD) titles can be re-matched — this one is a download")
    try:
        stream_url = Path(row.strm_path).read_text(encoding="utf-8").strip()
    except (OSError, TypeError):
        stream_url = ""
    key = stream_key_for_url(stream_url)
    if not key:
        raise WrongMatchError(409, "Could not read which provider stream this title plays (its .strm file is missing)")

    if db.query(Movie).filter(Movie.tmdb_id == new_tmdb_id).first() is not None:
        result = block_and_remove_movie(db, tmdb_id, user_name=user_name,
                                        reason=f"really tmdb:{new_tmdb_id}, already in library")
        result["message"] = "That film is already in your library — removed this duplicate copy"
        result["merged"] = True
        return result

    tmdb = _tmdb(db)
    new, failed = _tmdb_call(tmdb, tmdb.get_movie_details, new_tmdb_id)
    if not new:
        if failed:
            raise WrongMatchError(503, TMDB_DOWN)
        raise WrongMatchError(404, "TMDB has no movie with that id")

    old_meta = {"tmdb_id": row.tmdb_id, "title": row.title, "year": row.year, "genres": row.genres or [],
                "rating": row.rating, "runtime": row.runtime}
    source = row.source
    stale = set(get_list_tags_for_tmdb_id(row.tmdb_id, "movie", db)) \
        | set(apply_tag_rules(old_meta, "movie", source, row.source_tag, db))
    tags = [t for t in (row.tags or []) if t not in stale]
    new_meta = dict(new, tags=tags)
    for t in list(get_list_tags_for_tmdb_id(new_tmdb_id, "movie", db)) \
            + list(apply_tag_rules(new_meta, "movie", source, row.source_tag, db)):
        if t not in tags:
            tags.append(t)

    old_strm, old_title, old_jf = row.strm_path, row.title, row.jellyfin_item_id
    try:
        from services.sync import chown_path
    except Exception:  # pragma: no cover
        def chown_path(_p):
            return None
    if _folder_is_this_copys(db, row):
        return _rematch_in_place(db, row, key, new, tags, tmdb_id, new_tmdb_id, user_name, chown_path)
    # The copy shares its folder (a download there): it moves.
    root = Path(old_strm).parent.parent
    title, year = new.get("title") or "", new.get("year")
    folder = vod_folder_name(title, year)
    if _folder_taken(db, row, root / folder, new_tmdb_id):
        claimed = vod_folder_name(title, year, tag=f" [tmdbid-{new_tmdb_id}]")
        logger.info(f"[WrongMatch] '{folder}' belongs to another title; writing tmdb:{new_tmdb_id} "
                    f"to '{claimed}'")
        folder = claimed
    new_dir = root / folder
    new_strm, new_nfo = new_dir / f"{folder}.strm", new_dir / f"{folder}.nfo"
    new_dir.mkdir(parents=True, exist_ok=True)
    chown_path(new_dir)
    new_strm.write_text(stream_url, encoding="utf-8")
    chown_path(new_strm)
    write_movie_nfo(new_nfo, new, tags)
    chown_path(new_nfo)
    moved = Path(old_strm) != new_strm
    if moved:
        delete_movie_files(old_strm)
        if Path(old_strm).exists():
            delete_movie_files(str(new_strm))  # undo: one copy, not two
            raise WrongMatchError(500, "Couldn't delete the old .strm file, so nothing was changed. "
                                       "Check that Tentacle can write to the VOD folder.")

    _apply_rematch(db, row, key, new, tags, tmdb_id, new_tmdb_id, user_name, new_strm, new_nfo, None)
    _audit_rematch(db, key, old_title, row, tmdb_id, new_tmdb_id, user_name)

    # The old copy's files are gone: a scan drops its Jellyfin item (never a
    # DELETE -- see _delete_from_jellyfin) and brings in the new folder.
    # Same path (the right film has the same folder name): the item stays and
    # becomes the fixed film on the scan, so it keeps its playlist entries.
    _delete_from_jellyfin(db, tmdb_id, old_jf, old_strm, cleanup_playlists=moved)
    _refresh_caches()
    return {"ok": True, "title": row.title, "year": row.year, "tmdb_id": new_tmdb_id, "in_place": False,
            "message": f"Fixed: this is {row.title} ({row.year}). Jellyfin is picking it up now."}


def override_keys(db: Session, provider_id: int, media_type: str = "movie") -> dict:
    from models.database import MatchOverride
    return {o.stream_key: o.tmdb_id for o in db.query(MatchOverride).filter(
        MatchOverride.provider_id == provider_id, MatchOverride.media_type == media_type).all()}


def override_for(overrides: dict, stream_id, url: str = "") -> Optional[int]:
    if not overrides:
        return None
    if stream_id is not None and str(stream_id) in overrides:
        return overrides[str(stream_id)]
    if not url:
        return None
    if url in overrides:
        return overrides[url]
    # Keyed like is_blocked(): an M3U entry's key is derived from its URL.
    return overrides.get(stream_key_for_url(url))


# ── Stills from the stream ────────────────────────────────────────────────
# When the admin can't tell which film a stream is from its length and
# language, a few pictures from it settle it. ffmpeg seeks into the provider
# stream and grabs single frames; they're cached on disk per stream.

FRAME_WIDTH = 480
FRAME_TIMEOUT = 25        # seconds per frame (a provider seek can be slow)
_frames_lock = threading.Lock()


def frame_offsets(minutes: Optional[int]) -> list:
    """Seconds into the film to grab: spread over it when its length is known,
    past the opening credits either way."""
    if minutes and minutes >= 10:
        return [int(minutes * 60 * f) for f in (0.12, 0.4, 0.7)]
    return [5 * 60, 20 * 60, 45 * 60]


def _frame_cache_dir() -> Path:
    import os
    return Path(os.getenv("DATA_DIR", "/data")) / "frame_cache"


def _grab_frame(ffmpeg: str, url: str, user_agent: str, seconds: int) -> Optional[bytes]:
    import subprocess
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
           "-user_agent", user_agent, "-ss", str(seconds), "-i", url,
           "-frames:v", "1", "-vf", f"scale={FRAME_WIDTH}:-2",
           "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=FRAME_TIMEOUT)
    except subprocess.TimeoutExpired:
        logger.info(f"[WrongMatch] Frame at {seconds}s timed out")
        return None
    if out.returncode != 0 or not out.stdout:
        logger.info(f"[WrongMatch] Frame at {seconds}s failed: {out.stderr.decode(errors='replace')[-200:]}")
        return None
    return out.stdout


def _prune_frame_cache(cache: Path, max_age_days: int = 14) -> None:
    import time
    cutoff = time.time() - max_age_days * 86400
    try:
        for f in cache.glob("*.jpg"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def stream_frames(db: Session, tmdb_id: int) -> dict:
    """A few stills from this VOD movie's stream, as data URIs."""
    import base64
    import hashlib
    import shutil
    from models.database import Provider

    row = db.query(Movie).filter(Movie.tmdb_id == tmdb_id).first()
    if row is None:
        raise WrongMatchError(404, "This movie is not in Tentacle's library")
    try:
        url = Path(row.strm_path).read_text(encoding="utf-8").strip()
    except (OSError, TypeError):
        url = ""
    if not url.startswith(("http://", "https://")):
        raise WrongMatchError(409, "Could not read which stream this title plays (its .strm file is missing)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise WrongMatchError(503, "ffmpeg is not available on the Tentacle server")
    provider = db.query(Provider).filter(Provider.id == row.provider_id).first() if row.provider_id else None
    user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"
    minutes = probed_minutes(db, row) or row.runtime
    offsets = frame_offsets(minutes)

    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    cache = _frame_cache_dir()
    _prune_frame_cache(cache)

    def _cached(sec):
        path = cache / f"{key}_{sec}.jpg"
        try:
            return path.read_bytes() if path.exists() and path.stat().st_size else None
        except OSError:
            return None

    # Pictures grabbed before need no provider connection at all.
    cached = [(sec, _cached(sec)) for sec in offsets]
    if all(data for _, data in cached):
        return {"frames": [{"at_minutes": round(sec / 60),
                            "image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}
                           for sec, data in cached]}

    # Three ffmpeg seeks into the stream are three provider connections in a
    # row. On a connection-limited account that cuts off whatever is already
    # open -- usually a recording. A person pressed this button and can wait.
    from services.provider_activity import live_streams_active
    if live_streams_active():
        raise WrongMatchError(503, "Something is playing from your provider right now (live TV, a recording "
                                   "or a film). Grabbing pictures would open another provider connection and "
                                   "can cut it off — try again once it has finished.")

    frames = []
    # One grab at a time: IPTV providers often allow a single connection.
    with _frames_lock:
        for sec in offsets:
            path = cache / f"{key}_{sec}.jpg"
            data = None
            try:
                if path.exists() and path.stat().st_size:
                    data = path.read_bytes()
            except OSError:
                data = None
            if data is None:
                data = _grab_frame(ffmpeg, url, user_agent, sec)
                if data:
                    try:
                        cache.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                    except OSError as e:
                        logger.debug(f"[WrongMatch] Frame cache write failed: {e}")
            if data:
                frames.append({"at_minutes": round(sec / 60),
                               "image": "data:image/jpeg;base64," + base64.b64encode(data).decode()})
    if not frames:
        raise WrongMatchError(502, "Couldn't grab pictures from the stream — the provider may be busy "
                                   "(many allow only one stream at a time). Try again in a minute.")
    return {"frames": frames}
