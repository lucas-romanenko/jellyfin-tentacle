"""
Tentacle - Music Router

Search, album / artist / song pages, requests, the Library's Music tab, the
review list, the daily reconcile, status, and Lidarr's webhook with its
settings. Everything but settings answers 404 while the module is off, so
Tentacle shows no music anywhere.
"""
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import MusicAlbum, TentacleUser, get_db, get_setting, set_setting
from routers.auth import get_user_from_request, require_admin
from services.music import settings as music_settings
from services.music.settings import LIDARR_WEBHOOK_TRIGGERS, WEBHOOK_PATH

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/music", tags=["music"], dependencies=[Depends(require_admin)])
# Lidarr can't sign in: the webhook authenticates with its secret, in the handler.
webhook_router = APIRouter(prefix="/api/music", tags=["music"])

LAST_TEST_KEY = "music_webhook_last_test"


def _webhook_url(base: str, secret: str) -> str:
    return f"{base}{WEBHOOK_PATH}?secret={secret}" if base else ""


@router.get("/webhook-info")
def webhook_info(db: Session = Depends(get_db)):
    """What to paste into Lidarr → Settings → Connect → + → Webhook."""
    secret = music_settings.ensure_webhook_secret(db)
    base = music_settings.webhook_base_url(db)
    return {
        "base_url": base,  # "" = unknown; the page falls back to its own address
        "path": WEBHOOK_PATH,
        "secret": secret,
        "url": _webhook_url(base, secret),
        "method": "POST",
        "triggers": [label for _, label in LIDARR_WEBHOOK_TRIGGERS],
        "last_test_at": get_setting(db, LAST_TEST_KEY) or None,
    }


@router.post("/webhook/regenerate")
def regenerate_webhook_secret(db: Session = Depends(get_db)):
    """New secret. Lidarr's webhook URL has to be updated with it."""
    secret = music_settings.new_webhook_secret(db)
    base = music_settings.webhook_base_url(db)
    return {"secret": secret, "url": _webhook_url(base, secret)}


def _field(notification: dict, name: str):
    for f in notification.get("fields") or []:
        if f.get("name") == name:
            return f.get("value")
    return None


def find_tentacle_webhook(notifications: list) -> dict:
    """Lidarr's Webhook connection that points at Tentacle's music webhook, if any."""
    for n in notifications or []:
        if (n.get("implementation") or "").lower() != "webhook":
            continue
        url = str(_field(n, "url") or "")
        if WEBHOOK_PATH in url:
            return n
    return None


@router.post("/webhook/test")
def test_webhook(db: Session = Depends(get_db)):
    """Have Lidarr fire its own webhook test at Tentacle, then confirm it arrived.

    This tests the real path (Lidarr's saved settings → network → Tentacle's
    secret check), not just whether Tentacle answers.
    """
    from services.lidarr import LidarrClient, LidarrError
    from services.service_checks import Checks

    c = Checks("Lidarr webhook")
    url, key = get_setting(db, "lidarr_url"), get_setting(db, "lidarr_api_key")
    if not url or not key:
        c.fail("Lidarr connection", "Connect Lidarr first (Settings → Connections)")
        return c.result()
    client = LidarrClient(url, key)
    try:
        notifications = client.notifications()
    except LidarrError as e:
        c.fail("Lidarr connection", e.message)
        return c.result()
    c.ok("Lidarr connection", client.url)

    hook = find_tentacle_webhook(notifications)
    if not hook:
        c.fail("Webhook in Lidarr", f"No Webhook connection in Lidarr points at {WEBHOOK_PATH}. "
                                    "Add one with the settings shown above, then test again.")
        return c.result()
    c.ok("Webhook in Lidarr", f"'{hook.get('name') or 'Webhook'}'")

    secret = music_settings.ensure_webhook_secret(db)
    sent = (parse_qs(urlparse(str(_field(hook, "url") or "")).query).get("secret") or [""])[0]
    if not sent:
        c.fail("Secret", "Lidarr's webhook URL has no ?secret=. Copy the full URL shown above.")
    elif not hmac.compare_digest(sent.encode(), secret.encode()):
        c.fail("Secret", "Lidarr's webhook URL has an old secret. Copy the full URL shown above.")
    else:
        c.ok("Secret", "matches")

    off = [label for field, label in LIDARR_WEBHOOK_TRIGGERS if not hook.get(field)]
    if off:
        c.warn("Triggers", "Also tick " + ", ".join(off) + " in Lidarr's webhook, so Tentacle hears about new artists and imports.")
    else:
        c.ok("Triggers", ", ".join(label for _, label in LIDARR_WEBHOOK_TRIGGERS))

    before = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    try:
        client.test_notification(hook)
    except LidarrError as e:
        c.fail("Lidarr's test", e.message)
        return c.result()
    db.expire_all()
    arrived = get_setting(db, LAST_TEST_KEY) or ""
    if arrived and arrived >= before:
        c.ok("Lidarr reached Tentacle", "the test event arrived")
    else:
        c.fail("Lidarr reached Tentacle", "Lidarr says it sent the test, but it never arrived. Check the "
                                          "address in Lidarr's webhook URL is one Lidarr can reach.")
    return c.result("Lidarr's webhook reaches Tentacle")


@webhook_router.post("/webhook")
async def lidarr_webhook(request: Request, db: Session = Depends(get_db)):
    """Lidarr's Webhook connection posts here. The secret is always required."""
    secret = get_setting(db, "music_webhook_secret")
    provided = request.headers.get("X-Tentacle-Secret") or request.query_params.get("secret") or ""
    if not secret or not provided or not hmac.compare_digest(provided.encode(), secret.encode()):
        logger.warning("[Music webhook] Rejected a call with a missing or wrong secret")
        raise HTTPException(401, "Invalid or missing webhook secret")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    event = (payload or {}).get("eventType") or "?"
    if event == "Test":
        set_setting(db, LAST_TEST_KEY, datetime.now(timezone.utc).isoformat(timespec="microseconds"))
        logger.info("[Music webhook] Test event from Lidarr received")
        return {"ok": True, "event": "Test"}
    if not music_settings.is_enabled(db):
        return {"ok": True, "event": event, "ignored": "the music module is off"}
    from services.music import jobs, worker
    job, event = jobs.handle_webhook(payload)
    worker.submit(job, worker.NORMAL, f"webhook {event}")
    logger.info(f"[Music webhook] {event} received")
    return {"ok": True, "event": event, "queued": True}


# ── The module's pages (any signed-in user; 404 while the module is off) ──

def _is_plugin(request: Optional[Request]) -> bool:
    return bool(request is not None and request.query_params.get("api_key"))


def music_user(request: Request, db: Session = Depends(get_db),
               user: TentacleUser = Depends(get_user_from_request)) -> TentacleUser:
    """A signed-in user, with the module on. Inside Jellyfin (the plugin) music
    also needs the Jellyfin music integration on."""
    if not music_settings.is_enabled(db):
        raise HTTPException(404, "The music module is off")
    if _is_plugin(request) and (get_setting(db, "jellyfin_music_enabled", "false") or "").lower() != "true":
        raise HTTPException(404, "Music isn't enabled for Jellyfin")
    return user


def _via(request: Optional[Request]) -> str:
    if request is None:
        return "the API"
    return "the Jellyfin plugin" if _is_plugin(request) else "Tentacle's dashboard"


def _musicbrainz_errors(fn):
    """MusicBrainz or Lidarr trouble becomes a 502 with the reason, not a 500."""
    import functools
    from services.lidarr import LidarrError
    from services.music.library import MusicUnavailable
    from services.musicbrainz import MusicBrainzError

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except (MusicBrainzError, LidarrError) as e:
            raise HTTPException(e.status if e.status == 404 else 502, e.message)
        except MusicUnavailable as e:
            raise HTTPException(e.status, e.message)
    return wrapper


def _mbid(value: str) -> str:
    from services.musicbrainz import is_mbid
    if not is_mbid(value):
        raise HTTPException(400, "Not a MusicBrainz id")
    return value


@webhook_router.get("/config")
def music_config(request: Request, db: Session = Depends(get_db),
                 user: TentacleUser = Depends(get_user_from_request)):
    """Whether to show music at all. The Jellyfin plugin also needs the Jellyfin music integration."""
    from services.music.players import enabled_players
    enabled = music_settings.is_enabled(db)
    jellyfin = enabled and (get_setting(db, "jellyfin_music_enabled", "false") or "").lower() == "true"
    return {"enabled": enabled, "jellyfin": jellyfin, "show": jellyfin if _is_plugin(request) else enabled,
            "players": [{"id": p.id, "name": p.name} for p in enabled_players(db)] if enabled else []}


@webhook_router.get("/search")
@_musicbrainz_errors
def music_search(q: str, request: Request, db: Session = Depends(get_db),
                 user: TentacleUser = Depends(music_user)):
    from services.music import browse
    try:
        return browse.search(db, q, who=str(user.id if user else ""))
    except browse.StaleSearch:
        return {"stale": True, "artists": [], "albums": [], "songs": []}


@webhook_router.get("/album/{rgid}")
@_musicbrainz_errors
def music_album(rgid: str, db: Session = Depends(get_db), user: TentacleUser = Depends(music_user)):
    from services.music import browse
    return browse.album_page(db, _mbid(rgid))


@webhook_router.get("/artist/{mbid}")
@_musicbrainz_errors
def music_artist(mbid: str, db: Session = Depends(get_db), user: TentacleUser = Depends(music_user)):
    from services.music import browse
    return browse.artist_page(db, _mbid(mbid))


@webhook_router.get("/song")
@_musicbrainz_errors
def music_song(title: str, artist: str, db: Session = Depends(get_db), user: TentacleUser = Depends(music_user)):
    from services.music import browse
    if not title.strip():
        raise HTTPException(400, "No song title")
    return browse.song_page(db, title, _mbid(artist))


class AlbumRequest(BaseModel):
    mbid: str
    # For an album whose original is ambiguous: the user's pick.
    tracks: Optional[int] = None
    release_id: Optional[str] = None


@webhook_router.post("/request")
def music_request(body: AlbumRequest, request: Request, db: Session = Depends(get_db),
                  user: TentacleUser = Depends(music_user)):
    """Request an album. Goes through the single request path (services.media_requests)."""
    from services.media_requests import RequestRefused, request_album
    choice = {"release_id": body.release_id} if body.release_id else ({"tracks": body.tracks} if body.tracks else None)
    try:
        return request_album(db, body.mbid, user_id=user.id if user else None, via=_via(request), choice=choice)
    except RequestRefused as e:
        raise HTTPException(e.status, e.message)


@webhook_router.get("/library")
def music_library(status: str = "all", request: Request = None, db: Session = Depends(get_db),
                  user: TentacleUser = Depends(music_user)):
    """Monitored albums grouped by artist, with a status each."""
    from services.music import library
    try:
        client = library.lidarr_client(db)
    except library.MusicUnavailable:
        client = None
    queue_ids, progress = library.queued(client)
    rows = db.query(MusicAlbum).filter(MusicAlbum.monitored.is_(True)).all()
    albums = [library.album_summary(r, queue_ids, progress) for r in rows]
    counts = {"all": len(albums)}
    for a in albums:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    if status != "all":
        albums = [a for a in albums if a["status"] == status]
    by_artist = {}
    for a in sorted(albums, key=lambda a: (a["year"] or "9999", a["title"] or "")):
        by_artist.setdefault((a["artist"] or "", a["artist_mbid"]), []).append(a)
    from services.music.players import enabled_players
    return {"artists": [{"name": name, "mbid": mbid, "albums": items}
                        for (name, mbid), items in sorted(by_artist.items(), key=lambda kv: kv[0][0].lower())],
            "counts": counts, "players": [{"id": p.id, "name": p.name} for p in enabled_players(db)]}


@webhook_router.get("/open/{rgid}")
def music_open(rgid: str, player: str, db: Session = Depends(get_db), user: TentacleUser = Depends(music_user)):
    """"Open in <player>": resolve the album in that player and send the browser there."""
    from html import escape
    from services.music.players import enabled_players
    row = db.query(MusicAlbum).filter(MusicAlbum.mbid == _mbid(rgid)).first()
    target = next((p for p in enabled_players(db) if p.id == player), None)
    url = target.album_url(rgid, row.title if row else "") if target else None
    if url:
        return RedirectResponse(url, status_code=302)
    name = escape(target.name if target else player)
    return HTMLResponse(f"<p style='font-family:sans-serif'>{name} doesn't list this album yet. It may still be "
                        f"downloading, or {name} hasn't scanned it. <a href='javascript:history.back()'>Back</a></p>",
                        status_code=404)


# ── Admin: review, reconcile ─────────────────────────────────────────────

class ReviewChoice(BaseModel):
    tracks: Optional[int] = None
    release_id: Optional[str] = None


@router.post("/review/{rgid}")
def resolve_review(rgid: str, body: ReviewChoice, db: Session = Depends(get_db)):
    """Pick the tracklist for an album in review: pin it (and search if files are missing)."""
    from services.music import jobs, worker
    if not music_settings.is_enabled(db):
        raise HTTPException(404, "The music module is off")
    row = db.query(MusicAlbum).filter(MusicAlbum.mbid == _mbid(rgid)).first()
    if not row or not row.lidarr_album_id:
        raise HTTPException(404, "Lidarr doesn't have this album")
    if not body.tracks and not body.release_id:
        raise HTTPException(400, "Pick a tracklist")
    choice = {"release_id": body.release_id} if body.release_id else {"tracks": body.tracks}
    row.verdict = dict(row.verdict or {}, state="Pinning your choice…")
    db.commit()
    worker.submit(jobs.resolve_review(row.lidarr_album_id, rgid, choice), worker.URGENT, f"review {row.title}")
    return {"ok": True}


@router.post("/reconcile")
def start_reconcile(db: Session = Depends(get_db)):
    from services.music import jobs
    if not music_settings.is_enabled(db):
        raise HTTPException(404, "The music module is off")
    if not (get_setting(db, "lidarr_url") and get_setting(db, "lidarr_api_key")):
        raise HTTPException(400, "Connect Lidarr first (Settings → Connections)")
    started = jobs.start_reconcile("manual")
    return {"started": started, "message": "Checking the library" if started else "A check is already running"}


# ── Admin: the review page (phase 3) ────────────────────────────────────

def _module_on(db):
    if not music_settings.is_enabled(db):
        raise HTTPException(404, "The music module is off")


@router.get("/review")
def review_page(db: Session = Depends(get_db)):
    """Monitored albums that aren't right, by what fixing them takes; artists
    that need a picture chosen; albums already right but not locked."""
    from models.database import MusicArtist
    from services.music import library
    from services.music.jobs import AUTO_SETTINGS
    _module_on(db)
    groups = {c: [] for c in ("repin", "repin_trim", "repin_download", "review")}
    for row in db.query(MusicAlbum).filter(MusicAlbum.monitored.is_(True),
                                           MusicAlbum.category.in_(list(groups))).all():
        item = library.album_summary(row)
        v = row.verdict or {}
        item.update(target=v.get("target"), pinned=v.get("pinned"), have=v.get("have"),
                    state=v.get("state") if v.get("state") == "Applying…" else None)
        groups[row.category].append(item)
    for items in groups.values():
        items.sort(key=lambda a: ((a["artist"] or "").lower(), a["year"] or "", a["title"] or ""))
    unlocked = db.query(MusicAlbum).filter(MusicAlbum.category == "right", MusicAlbum.monitored.is_(True),
                                           MusicAlbum.any_release_ok.is_(True)).count()
    pictures = [{"mbid": a.mbid, "name": a.name, "disambiguation": a.disambiguation or "",
                 "note": a.picture_note or "", "status": a.picture_status,
                 "candidates": a.picture_candidates or []}
                for a in db.query(MusicArtist).filter(MusicArtist.picture_status.in_(("review", "error")))
                .order_by(MusicArtist.name).all()]
    auto = {c: (get_setting(db, k, "false") or "").lower() == "true" for c, k in AUTO_SETTINGS.items()}
    return {"groups": groups, "unlocked": unlocked, "pictures": pictures, "auto": auto,
            "recycle_bin": _recycle_bin(db) if groups["repin_trim"] else None}


_recycle_bin_cache = {"at": None, "value": None}


def _recycle_bin(db) -> Optional[str]:
    """Lidarr's recycle bin ("" = none set, None = couldn't ask), cached for 5 minutes
    so the review page stays cheap."""
    import time
    from services.lidarr import LidarrError
    from services.music import library
    at = _recycle_bin_cache["at"]
    if at is not None and time.monotonic() - at < 300:
        return _recycle_bin_cache["value"]
    try:
        value = library.lidarr_client(db).recycle_bin()
    except (LidarrError, library.MusicUnavailable) as e:
        logger.debug(f"[Music] couldn't read Lidarr's recycle bin setting: {getattr(e, 'message', e)}")
        return None
    _recycle_bin_cache.update(at=time.monotonic(), value=value)
    return value


class ApplyRequest(BaseModel):
    mbids: Optional[list] = None
    category: Optional[str] = None   # apply a whole group


@router.post("/apply")
def apply_fixes(body: ApplyRequest, db: Session = Depends(get_db)):
    """Apply per album or per group. Queued; one album at a time; each re-checked first."""
    from services.music import jobs, worker
    _module_on(db)
    if body.category:
        if body.category not in jobs.AUTO_SETTINGS:
            raise HTTPException(400, "That group can't be applied (needs review is decided album by album)")
        mbids = [r.mbid for r in db.query(MusicAlbum).filter(MusicAlbum.category == body.category,
                                                              MusicAlbum.monitored.is_(True)).all()]
    else:
        mbids = [m for m in (body.mbids or []) if isinstance(m, str)]
    if not mbids:
        raise HTTPException(400, "Nothing to apply")
    rows = db.query(MusicAlbum).filter(MusicAlbum.mbid.in_(mbids)).all()
    for row in rows:
        row.verdict = dict(row.verdict or {}, state="Applying…")
    db.commit()
    worker.submit(jobs.apply_albums(mbids), worker.URGENT, f"apply {len(mbids)} album(s)")
    return {"queued": len(mbids)}


@router.post("/lock")
def lock_right(db: Session = Depends(get_db)):
    """Turn "any release OK" off for albums already pinned right (changes no files)."""
    from services.music import jobs, worker
    _module_on(db)
    worker.submit(jobs.lock_albums(), worker.URGENT, "lock right albums")
    return {"queued": True}


MAX_PICTURE = 10 * 1024 * 1024


@router.post("/artist/{mbid}/picture")
async def upload_artist_picture(mbid: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """The review page's upload button: this picture, set in every player."""
    from models.database import MusicArtist
    from services.music import jobs, worker
    _module_on(db)
    if not db.query(MusicArtist).filter(MusicArtist.mbid == _mbid(mbid)).first():
        raise HTTPException(404, "Unknown artist")
    data = await file.read(MAX_PICTURE + 1)
    if len(data) > MAX_PICTURE:
        raise HTTPException(413, "That picture is over 10 MB")
    from services.music.pictures import looks_like_placeholder
    if looks_like_placeholder(data):
        raise HTTPException(400, "That isn't a photo (it can't be read, or it's a flat graphic)")
    worker.submit(jobs.picture_for(mbid, data=data), worker.URGENT, "artist picture")
    return {"queued": True}


class DeezerPick(BaseModel):
    deezer_id: int


@router.post("/artist/{mbid}/picture/deezer")
def pick_deezer_picture(mbid: str, body: DeezerPick, db: Session = Depends(get_db)):
    """Use one of the Deezer candidates shown on the review page."""
    from models.database import MusicArtist
    from services.music import jobs, worker
    _module_on(db)
    artist = db.query(MusicArtist).filter(MusicArtist.mbid == _mbid(mbid)).first()
    if not artist:
        raise HTTPException(404, "Unknown artist")
    pick = next((c for c in artist.picture_candidates or [] if c.get("id") == body.deezer_id), None)
    if not pick or not pick.get("picture"):
        raise HTTPException(400, "That isn't one of the candidates shown")
    worker.submit(jobs.picture_for(mbid, deezer_url=pick["picture"]), worker.URGENT, "artist picture")
    return {"queued": True}


@router.post("/pictures/check")
def check_pictures(db: Session = Depends(get_db)):
    from services.music import jobs, worker
    _module_on(db)
    worker.submit(jobs.pictures_job(), worker.NORMAL, "artist pictures")
    return {"queued": True}


# ── Status (for monitoring) ──────────────────────────────────────────────

def status_access(request: Request, db: Session = Depends(get_db)):
    """An admin, the internal secret, or the music webhook secret (?secret=), so a
    monitor that can't sign in can poll it with the secret shown in Settings → Music."""
    from routers.auth import require_internal_or_admin
    provided = request.headers.get("X-Tentacle-Secret") or request.query_params.get("secret") or ""
    secret = get_setting(db, "music_webhook_secret") or ""
    if provided and secret and hmac.compare_digest(provided.encode(), secret.encode()):
        return None
    return require_internal_or_admin(request, db)


@webhook_router.get("/status", dependencies=[Depends(status_access)])
def music_status(db: Session = Depends(get_db)):
    """Last reconcile, counts per category, needs review, last error, integration health."""
    from services.music import status
    return status.status(db)
