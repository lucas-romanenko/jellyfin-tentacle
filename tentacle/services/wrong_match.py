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
    """Whether a catalogue entry is blocked — by id, or by URL for M3U."""
    if not keys:
        return False
    if stream_id is not None and str(stream_id) in keys:
        return True
    return bool(url) and url in keys


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
    (Jellyfin mounts the VOD folder elsewhere, so the prefix differs)."""
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
                          strm_path: Optional[str] = None) -> bool:
    url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return False
    try:
        from services.jellyfin import JellyfinService
        jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
        item = jellyfin_item_for_strm(jf, tmdb_id, strm_path, jf_item_id)
        if item is None:
            # Not found as this file: leave every other item alone. The .strm
            # is gone from disk, so Jellyfin drops the item on its next scan.
            logger.info(f"[WrongMatch] No Jellyfin item is this copy's .strm (tmdb:{tmdb_id}); "
                        f"nothing deleted in Jellyfin, a library scan will drop it")
            try:
                jf.trigger_library_scan(None)
            except Exception:
                pass
            return False
        jf_item_id = item["Id"]
        ok = jf.delete_item(jf_item_id)
    except Exception as e:
        logger.warning(f"[WrongMatch] Could not delete tmdb:{tmdb_id} from Jellyfin: {e}")
        return False
    if ok:
        from routers.library import _cleanup_playlists_all_users
        threading.Thread(target=_cleanup_playlists_all_users,
                         args=(tmdb_id, "movie", jf_item_id), daemon=True).start()
    return ok


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
        sources = item.get("MediaSources") or []
        ticks = (sources[0].get("RunTimeTicks") if sources else None) or 0
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
        sources = item.get("MediaSources") or []
        ticks = (sources[0].get("RunTimeTicks") if sources else None) or 0
        langs = []
        for s in (sources[0].get("MediaStreams") or []) if sources else []:
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

    seen, found = {tmdb_id}, []
    for q in queries:
        data = tmdb._request("search/movie", {"query": q}) or {}
        for r in (data.get("results") or [])[:10]:
            if r.get("id") in seen:
                continue
            seen.add(r["id"])
            found.append(r)
        if len(found) >= 24:
            break

    in_lib = {t for (t,) in db.query(Movie.tmdb_id).filter(Movie.tmdb_id.in_([r["id"] for r in found])).all()} if found else set()
    label = (row.title or "").lower()
    candidates = []
    for r in found[:24]:
        details = tmdb.get_movie_details(r["id"]) or {}
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
        # Length first (when known), then the audio language, then how much
        # of the label it shares, then fame.
        return (0 if c["runtime_matches"] else 1, 0 if c["language_matches"] else 1,
                gap if actual else 0, -c["_sim"], -c["_pop"])

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


def rematch_movie(db: Session, tmdb_id: int, new_tmdb_id: int, user_name: str = None) -> dict:
    """This VOD stream is really `new_tmdb_id`: move the copy to that film.

    Same stream, new identity: the .strm and NFO move to the right film's
    folder with its metadata, tags tied to the old film (lists, rules) are
    recomputed, and a MatchOverride keeps the sync from undoing it. If the
    right film is already in the library, this copy is simply a duplicate —
    it is removed and its stream blocked instead.
    """
    from services.media_files import delete_movie_files
    from services.nfo import vod_folder_name, write_movie_nfo
    from services.tagger import apply_tag_rules, get_list_tags_for_tmdb_id
    from models.database import Duplicate, MatchOverride

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

    new = _tmdb(db).get_movie_details(new_tmdb_id)
    if not new:
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
    root = Path(old_strm).parent.parent
    folder = vod_folder_name(new.get("title") or "", new.get("year"))
    new_dir = root / folder
    new_strm, new_nfo = new_dir / f"{folder}.strm", new_dir / f"{folder}.nfo"
    try:
        from services.sync import chown_path
    except Exception:  # pragma: no cover
        def chown_path(_p):
            return None
    new_dir.mkdir(parents=True, exist_ok=True)
    chown_path(new_dir)
    new_strm.write_text(stream_url, encoding="utf-8")
    chown_path(new_strm)
    write_movie_nfo(new_nfo, new, tags)
    chown_path(new_nfo)
    if Path(old_strm) != new_strm:
        delete_movie_files(old_strm)

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
    row.jellyfin_item_id = None
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

    log_deletion(db, kind="rematch", name=old_title, media_type="movie", reason="manual", user_name=user_name,
                 detail=f"Stream {key if key.isdigit() else '(URL)'} re-matched: '{old_title}' (tmdb:{tmdb_id}) "
                        f"→ '{row.title}' (tmdb:{new_tmdb_id})")
    logger.info(f"[WrongMatch] Re-matched stream {key if key.isdigit() else '(URL)'}: '{old_title}' → "
                f"'{row.title}' ({row.year}) by {user_name}")

    # The old Jellyfin item's files are gone; remove it now (its delete hook finds
    # nothing under the old id — the row carries the new one) and scan so the
    # new folder comes in with the right metadata.
    _delete_from_jellyfin(db, tmdb_id, old_jf, old_strm)
    jf = _jf(db)
    if jf is not None:
        try:
            jf.trigger_library_scan(None)
        except Exception as e:
            logger.debug(f"[WrongMatch] Library scan request failed: {e}")
    _refresh_caches()
    return {"ok": True, "title": row.title, "year": row.year, "tmdb_id": new_tmdb_id,
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
    return overrides.get(url) if url else None


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
    # Three ffmpeg seeks into the stream are three provider connections in a
    # row. On a connection-limited account that cuts off whatever is already
    # open -- usually a recording. A person pressed this button and can wait.
    from services.provider_activity import live_streams_active
    if live_streams_active():
        raise WrongMatchError(503, "A live stream or recording is running right now. Grabbing pictures "
                                   "would open another provider connection and can cut it off — "
                                   "try again once it has finished.")

    provider = db.query(Provider).filter(Provider.id == row.provider_id).first() if row.provider_id else None
    user_agent = (provider.user_agent if provider else None) or "TiviMate/4.7.0 (Linux; Android 12)"
    minutes = probed_minutes(db, row) or row.runtime
    offsets = frame_offsets(minutes)

    key = hashlib.sha1(url.encode()).hexdigest()[:16]
    cache = _frame_cache_dir()
    _prune_frame_cache(cache)
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
