"""Artist pictures, set through the player integrations.

Source, in order:
1. an existing artist.jpg in the artist's folder, if it is a real picture
   (read-only, and only when the "music folder as Tentacle sees it" setting
   is set);
2. Deezer, by exact name, after dropping a clarifying note such as
   "Crowbar (Canadian band)".

An artist MusicBrainz tells apart with a clarifying note (there is more than
one band of that name), or one with no exact or several exact matches on
Deezer, goes to review with an upload button instead of risking another
band's photo. Placeholders are rejected: Last.fm's grey star, Deezer's blank
picture (/artist// in its URL), and any image that is a flat grey-and-white
graphic rather than a photo (Navidrome's own placeholder too).

A picture is only set in a player that shows none (Navidrome serves its
placeholder, Jellyfin has no primary image), so pictures that already work
are left alone. Navidrome's upload is its highest-priority artist image, which
is also what gets past its grey-star bug (navidrome/navidrome#5823; verified
on Big Star, Sept 2026).
"""
import io
import logging
import os
import re
from datetime import datetime
from typing import Optional

import requests

from models.database import MusicArtist, get_setting

logger = logging.getLogger(__name__)

TIMEOUT = 15
DEEZER_API = "https://api.deezer.com"
LASTFM_PLACEHOLDER_ID = "2a96cbd8b46e442fc41c2b86b821562f"
MAX_BYTES = 10 * 1024 * 1024


# ── Placeholders ─────────────────────────────────────────────────────────

def is_placeholder_url(url: str) -> bool:
    url = url or ""
    return "/artist//" in url or LASTFM_PLACEHOLDER_ID in url


def looks_like_placeholder(data: bytes) -> bool:
    """True for anything that isn't a usable photo: undecodable, or a flat graphic.

    Measured on real images: Navidrome's and Last.fm's grey stars have 2
    distinct colours at 16 levels per channel; every photo checked, black and
    white ones included, has 16 or more.
    """
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            small = im.convert("RGB").resize((64, 64))
    except Exception:
        return True
    coarse = {(r >> 4, g >> 4, b >> 4) for r, g, b in small.getdata()}
    return len(coarse) <= 4


# ── Names ────────────────────────────────────────────────────────────────

_NOTE = re.compile(r"\s*\([^)]*\)\s*$")


def strip_note(name: str) -> str:
    """ "Crowbar (Canadian band)" -> "Crowbar"."""
    return _NOTE.sub("", name or "").strip()


def same_name(a: str, b: str) -> bool:
    from services.music.browse import normalize
    return normalize(strip_note(a)) == normalize(strip_note(b)) != ""


# ── Sources ──────────────────────────────────────────────────────────────

def deezer_candidates(name: str, limit: int = 6) -> list:
    r = requests.get(f"{DEEZER_API}/search/artist", params={"q": strip_note(name), "limit": limit}, timeout=TIMEOUT)
    r.raise_for_status()
    found = [{"id": a.get("id"), "name": a.get("name"), "picture": a.get("picture_xl") or a.get("picture_big"),
              "thumb": a.get("picture_medium"), "albums": a.get("nb_album"), "fans": a.get("nb_fan")}
             for a in (r.json().get("data") or []) if a.get("id")]
    # Deezer's blank picture is never worth offering.
    return [c for c in found if c["picture"] and not is_placeholder_url(c["picture"])
            and not is_placeholder_url(c.get("thumb") or "")]


def review_candidates(name: str, candidates: list) -> list:
    """What the review picker offers: the exact-name artists when there are any,
    otherwise everything Deezer found (the name may be spelled differently there)."""
    exact = [c for c in candidates if exact_deezer_match(name, c["name"])]
    return exact or candidates


# Deezer often keeps a second profile under exactly the same name (Dire Straits:
# 2.3M fans and 1,489; Beck: 413k and 32). One with this many times the fans of
# the next exact match, and at least this many fans, is the artist.
DOMINANT_RATIO, DOMINANT_MIN_FANS = 10, 1000


def exact_deezer_match(our_name: str, deezer_name: str) -> bool:
    """Exact name match. Only OUR clarifying note is dropped: Deezer's "Billy Joel
    (Karaoke)" is not Billy Joel."""
    from services.music.browse import normalize
    return normalize(strip_note(our_name)) == normalize(deezer_name) != ""


def choose_deezer(name: str, candidates: list) -> tuple:
    """(picture url or None, why not)."""
    exact = [c for c in candidates if exact_deezer_match(name, c["name"]) and c.get("picture")
             and not is_placeholder_url(c["picture"])]
    if len(exact) == 1:
        return exact[0]["picture"], ""
    if not exact:
        return None, f"Deezer has no artist called exactly “{strip_note(name)}” with a picture."
    ranked = sorted(exact, key=lambda c: c.get("fans") or 0, reverse=True)
    top, runner = ranked[0].get("fans") or 0, ranked[1].get("fans") or 0
    if top >= DOMINANT_MIN_FANS and top >= DOMINANT_RATIO * max(runner, 1):
        return ranked[0]["picture"], ""
    return None, f"Deezer has {len(exact)} artists called “{strip_note(name)}”; pick the right one."


def download(url: str) -> bytes:
    r = requests.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    if len(r.content) > MAX_BYTES:
        raise ValueError("picture too large")
    return r.content


def local_artist_jpg(db, artist_path: str) -> Optional[bytes]:
    """artist.jpg from the artist's folder, read-only, via the music folder mapping."""
    base = (get_setting(db, "music_library_path") or "").strip().rstrip("/")
    root = (get_setting(db, "lidarr_root_folder") or "").strip().rstrip("/")
    if not base or not root or not artist_path or not artist_path.startswith(root + "/"):
        return None
    folder = os.path.realpath(base + artist_path[len(root):])
    if not folder.startswith(os.path.realpath(base) + os.sep):
        return None
    for name in ("artist.jpg", "artist.jpeg", "artist.png"):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= MAX_BYTES:
                with open(path, "rb") as f:
                    return f.read()
        except OSError:
            continue
    return None


# ── Per artist ───────────────────────────────────────────────────────────

class NeedsReview(Exception):
    def __init__(self, message: str, candidates: list = None):
        super().__init__(message)
        self.message = message
        self.candidates = candidates or []


def find_picture(db, artist: MusicArtist) -> tuple:
    """(image bytes, source). Raises NeedsReview."""
    data = local_artist_jpg(db, artist.path)
    if data and not looks_like_placeholder(data):
        return data, "artist.jpg"
    if (get_setting(db, "deezer_enabled", "true") or "").lower() != "true":
        raise NeedsReview("No real artist.jpg, and Deezer is off as a picture source. Upload one.")
    candidates = deezer_candidates(artist.name)
    if artist.disambiguation:
        raise NeedsReview(f"MusicBrainz knows more than one “{strip_note(artist.name)}” (this one: "
                          f"{artist.disambiguation}). Pick the picture by hand so it isn't another band's.",
                          review_candidates(artist.name, candidates))
    url, why = choose_deezer(artist.name, candidates)
    if not url:
        raise NeedsReview(why, review_candidates(artist.name, candidates))
    data = download(url)
    if looks_like_placeholder(data):
        raise NeedsReview("Deezer's picture for this artist is a placeholder. Upload one.",
                          review_candidates(artist.name, candidates))
    return data, "deezer"


def _picture_players(db) -> list:
    from services.music.players import enabled_players
    wanted = []
    for p in enabled_players(db):
        key = {"navidrome": "navidrome_upload_artist_images", "jellyfin": "jellyfin_music_set_artist_images"}.get(p.id)
        if key and (get_setting(db, key, "true") or "").lower() == "true":
            wanted.append(p)
    return wanted


def store_copy(db, artist: MusicArtist, data: bytes) -> None:
    """Keep the picture, so a player enabled later can get it too."""
    path = _copy_path(db, artist)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    except OSError as e:
        logger.debug(f"[Music] couldn't keep a copy of {artist.name}'s picture: {e}")


def _copy_path(db, artist: MusicArtist) -> str:
    return os.path.join(get_setting(db, "data_dir", "/data"), "music_pictures", f"{artist.mbid}.img")


def load_copy(db, artist: MusicArtist) -> Optional[bytes]:
    try:
        with open(_copy_path(db, artist), "rb") as f:
            return f.read()
    except OSError:
        return None


def ensure_picture(db, artist: MusicArtist, data: bytes = None, source: str = None, force: bool = False) -> str:
    """Make sure every picture-setting player shows a real picture of this artist.

    Normally a picture goes only to players that show none. force=True (the
    user uploaded or picked one) sets `data` everywhere. A picture the user
    chose earlier is kept and used again for a player that shows none (one
    that hadn't scanned the artist yet, say). Returns the new picture_status.
    """
    players = _picture_players(db)
    artist.picture_checked_at = datetime.utcnow()
    if not players:
        db.commit()
        return artist.picture_status or ""
    if data is None and artist.picture_source == "upload":
        data, source = load_copy(db, artist), "upload"
    missing, waiting = [], []
    for p in players:
        try:
            state = p.artist_image_state(artist.mbid, artist.name)
        except Exception as e:
            artist.picture_status, artist.picture_note = "error", f"{p.name}: {getattr(e, 'message', e)}"
            db.commit()
            return "error"
        if state == "absent":
            waiting.append(p.name)
        elif state == "missing" or force:
            missing.append(p)
    wait_note = f"{', '.join(waiting)} hasn't scanned this artist yet." if waiting else None
    if not missing:
        if waiting:
            artist.picture_status = "waiting"
        elif artist.picture_status not in ("set",):
            artist.picture_status = "ok"
        artist.picture_note = wait_note
        db.commit()
        return artist.picture_status
    if data is None:
        try:
            data, source = find_picture(db, artist)
        except NeedsReview as e:
            artist.picture_status, artist.picture_note, artist.picture_candidates = "review", e.message, e.candidates
            db.commit()
            return "review"
        except (requests.RequestException, ValueError) as e:
            artist.picture_status, artist.picture_note = "error", f"Couldn't get a picture: {e}"
            db.commit()
            return "error"
    errors = []
    for p in missing:
        try:
            p.set_artist_image(artist.mbid, artist.name, data)
        except Exception as e:
            errors.append(f"{p.name}: {getattr(e, 'message', e)}")
    store_copy(db, artist, data)
    artist.picture_source = source or artist.picture_source
    if errors:
        artist.picture_status, artist.picture_note = "error", "; ".join(errors)
    else:
        artist.picture_status = "waiting" if waiting else "set"
        artist.picture_note = wait_note
        artist.picture_candidates = None
        logger.info(f"[Music] {artist.name}: picture set in {', '.join(p.name for p in missing)} (from {source})")
    db.commit()
    return artist.picture_status
