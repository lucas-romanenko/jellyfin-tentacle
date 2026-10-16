"""A provider's new address or login reaches its VOD .strm files (#142).

A direct .strm is {server}/movie|series/{user}/{pass}/{id}.{ext}, written once
by the sync, and saving the provider changes only the provider row: every film
and episode already in the library kept the old, dead host. After a save that
changes the server or login, relink_strms rewrites this provider's own direct
files from the old form to the one the sync writes today. Anything else (a
Tentacle VOD link, which reads the provider's address when played, a proxy, a
hand-made file, another provider's rows) is left alone.

#149: the save also records the old address (setting provider_relink_pending)
until the rewrite has run, so a restart before it ran loses nothing
(resume_pending, at start-up). Every Xtream sync runs it first, before any
request to the provider (relink_before_sync): files that still carry the
provider's current login on a former host follow even when the provider
refuses the whole sync. An M3U provider's new playlist address is recorded the
same way, and its next full sync rewrites every listed file that differs.
"""
import json
import logging
import re
import threading
import time
from pathlib import Path

from sqlalchemy.orm import Session

from models.database import Movie, Provider, Series
from services.xtream_client import quote_cred

logger = logging.getLogger(__name__)

SYNC_WAIT_POLL_SECONDS = 5.0
SYNC_WAIT_MAX_SECONDS = 6 * 3600


def _login(provider) -> tuple:
    return (provider.server_url or "", provider.username or "", provider.password or "")


def _old_url_re(before: tuple):
    """A direct URL with the old server and login, quoted (today's form) or
    raw (written before #529)."""
    server, user, password = before
    logins = {f"{quote_cred(user)}/{quote_cred(password)}", "/".join((user, password))}
    alts = "|".join(re.escape(x) for x in sorted(logins))
    return re.compile(rf"^{re.escape(server.rstrip('/'))}/(movie|series)/(?:{alts})/(\d+)\.([A-Za-z0-9]+)$")


def _moved_matcher(db: Session, provider):
    """(kind, id, ext) of a direct URL with the provider's current login on
    another host (the provider moved), else None: the rule the sync's
    _strm_needs_rewrite applies to listed titles (sync._MOVED_RE). Not on
    another configured provider's host with the same login: that file plays
    the other provider (a takeover the sync heals by stream, #154)."""
    from urllib.parse import urlparse
    from services.sync import _MOVED_RE
    user, password = provider.username or "", provider.password or ""
    host = (urlparse(provider.server_url or "").hostname or "").lower()
    logins = {(quote_cred(user), quote_cred(password)), (user, password)}
    others = {(urlparse(p.server_url or "").hostname or "").lower()
              for p in db.query(Provider).filter(Provider.id != provider.id).all()
              if (p.username or "", p.password or "") == (user, password)}

    def match(url_text: str):
        m = _MOVED_RE.match(url_text)
        if (m is None or not host or m.group(1).lower() in {host} | others
                or (m.group(3), m.group(4)) not in logins):
            return None
        return m.group(2).lower(), m.group(5), url_text.rsplit(".", 1)[1]
    return match


def relink_strms(db: Session, provider: Provider, before: tuple = None) -> dict:
    """Rewrite the provider's direct .strm files that still carry `before`
    ((server, user, pass) from before the save, as for
    rewrite_xtream_channel_urls), or the provider's current login on another
    host. With `before` None only the latter (#149: no old address known).
    Returns {"movies": n, "episodes": n}."""
    from services.sync import XtreamClient, _write_strm, chown_path, vod_links_for

    counts = {"movies": 0, "episodes": 0}
    if (not provider.server_url or (provider.provider_type or "xtream") != "xtream"
            or not (provider.username or provider.password)):
        return counts
    old = _old_url_re(before) if before and before[0] and tuple(before) != _login(provider) else None
    moved = _moved_matcher(db, provider)
    client = XtreamClient(provider)
    client.vod_links = vod_links_for(db, provider)

    def relink(strm: Path, key: str) -> None:
        try:
            current = strm.read_text(encoding="utf-8").strip()
            m = old.match(current) if old is not None else None
            ref = (m.group(1).lower(), m.group(2), m.group(3)) if m is not None else moved(current)
            if ref is None:
                return
            kind, stream_id, ext = ref
            url = (client.movie_stream_url(stream_id, ext) if kind == "movie"
                   else client.episode_stream_url(stream_id, ext))
            if url == current:
                return
            _write_strm(strm, url)
            chown_path(strm)
            counts[key] += 1
        except (OSError, UnicodeDecodeError) as e:
            logger.debug(f"[Relink] Skipped {strm}: {e}")

    for movie in db.query(Movie).filter(Movie.provider_id == provider.id, Movie.strm_path.isnot(None)).all():
        if not movie.strm_disabled:
            relink(Path(movie.strm_path), "movies")
    for series in db.query(Series).filter(Series.provider_id == provider.id, Series.strm_path.isnot(None)).all():
        if series.strm_disabled:
            continue
        try:
            files = sorted(Path(series.strm_path).rglob("*.strm"))
        except OSError:
            continue
        for strm in files:
            relink(strm, "episodes")
    if counts["movies"] or counts["episodes"]:
        logger.info(f"[Relink] {provider.name}: {counts['movies']} films and {counts['episodes']} episodes "
                    f"now use the saved server and login")
    return counts


def _wait_for_sync(provider_id: int) -> None:
    """A sync of this provider that started with the old address would write
    after us: let it finish first (whatever it still wrote, the next sync
    heals: _strm_needs_rewrite)."""
    from routers.sync import _running_syncs
    waited = 0.0
    while provider_id in _running_syncs and waited < SYNC_WAIT_MAX_SECONDS:
        time.sleep(SYNC_WAIT_POLL_SECONDS)
        waited += SYNC_WAIT_POLL_SECONDS


def _run(provider_id: int, before: tuple) -> None:
    from models.database import SessionLocal, get_setting, log_activity
    _wait_for_sync(provider_id)
    db = SessionLocal()
    try:
        provider = db.query(Provider).filter(Provider.id == provider_id).first()
        if provider is None:
            clear_pending(db, provider_id)
            return
        counts = relink_strms(db, provider, before)
        clear_pending(db, provider_id, kind="xtream")
        if not (counts["movies"] or counts["episodes"]):
            return
        # Jellyfin keeps the old URL until a scan sees the file changed
        url, key = get_setting(db, "jellyfin_url", ""), get_setting(db, "jellyfin_api_key", "")
        if url and key:
            try:
                from services.jellyfin import JellyfinService
                JellyfinService(url, key, get_setting(db, "jellyfin_user_id", "")).trigger_library_scan(None)
            except Exception as e:
                logger.warning(f"[Relink] Jellyfin library scan failed: {e}")
        log_activity(db, "provider_relinked",
                     f"{counts['movies']} films and {counts['episodes']} episodes now use "
                     f"{provider.name}'s new address", counts)
    except Exception as e:
        logger.error(f"[Relink] Rewriting provider {provider_id}'s .strm files failed: {e}")
    finally:
        db.close()


def start_relink(provider_id: int, before: tuple) -> None:
    """relink_strms in the background (a large library takes a while), then a
    Jellyfin scan and an Activity entry."""
    threading.Thread(target=_run, args=(provider_id, before), daemon=True,
                     name=f"provider-relink-{provider_id}").start()


# ── What a save still owes, across restarts (#149) ───────────────────────
# Setting provider_relink_pending: JSON {provider_id: entry}. An Xtream
# provider's entry is [server, user, pass], its address before the save (the
# one its files carry); an M3U provider's is {"m3u": true} (its playlist moved).

PENDING_KEY = "provider_relink_pending"
M3U_TYPES = ("m3u_url", "m3u_file")


def pending(db: Session) -> dict:
    from models.database import get_setting
    try:
        value = json.loads(get_setting(db, PENDING_KEY, "") or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _store(db: Session, entries: dict) -> None:
    """Into the session only: the caller commits (a save, in its own commit)."""
    from models.database import Setting
    row = db.query(Setting).filter(Setting.key == PENDING_KEY).first()
    if row is None:
        db.add(Setting(key=PENDING_KEY, value=json.dumps(entries)))
    else:
        row.value = json.dumps(entries)


def address_changed(provider: Provider, before: tuple, m3u_before) -> bool:
    """A save moved what the provider's files play: an Xtream provider's
    server or login, an M3U provider's playlist address."""
    if (provider.provider_type or "xtream") in M3U_TYPES:
        return (provider.m3u_url or "") != (m3u_before or "")
    return _login(provider) != tuple(before)


def record_pending(db: Session, provider: Provider, before: tuple = None) -> None:
    """A save changed the provider's address or login: what its files must
    follow, kept until the rewrite has run. Xtream: `before`, the address
    before the save; a second save before the rewrite ran keeps the first one
    (the address the files still carry). M3U: a marker for its next sync.
    Not committed: the save's own commit carries it."""
    entries = pending(db)
    key = str(provider.id)
    if (provider.provider_type or "xtream") in M3U_TYPES:
        entries[key] = {"m3u": True}
    elif before and before[0]:
        if not isinstance(entries.get(key), list):
            entries[key] = list(before)
    else:
        return
    _store(db, entries)


def clear_pending(db: Session, provider_id: int, kind: str = None) -> None:
    """Drop the provider's entry; with `kind` ("xtream", "m3u") only one of that kind."""
    entries = pending(db)
    entry = entries.get(str(provider_id))
    if entry is None:
        return
    if (kind == "xtream" and not isinstance(entry, list)) or (kind == "m3u" and not isinstance(entry, dict)):
        return
    del entries[str(provider_id)]
    _store(db, entries)
    db.commit()


def forget(db: Session, provider_id: int) -> None:
    """Drop a deleted provider's entry, into the session only (the delete
    commits): the next provider may get its id (#625)."""
    entries = pending(db)
    if entries.pop(str(provider_id), None) is not None:
        _store(db, entries)


def relink_before_sync(db: Session, provider: Provider) -> dict:
    """Run by every sync before its first request to the provider (#149):
    repoint an Xtream provider's files to its current address (a pending
    save's old address, and its current login on any other host), then clear
    the pending entry. Local file work only, so a provider that refuses the
    sync still gets its library repointed; the Jellyfin scan after the sync
    picks the files up."""
    counts = {"movies": 0, "episodes": 0}
    if (provider.provider_type or "xtream") in M3U_TYPES:
        return counts
    entry = pending(db).get(str(provider.id))
    before = tuple(entry) if isinstance(entry, list) and len(entry) == 3 else None
    try:
        counts = relink_strms(db, provider, before)
    except Exception as e:
        logger.warning(f"[Relink] {provider.name}: repointing files before the sync failed: {e}")
        return counts
    if before is not None:
        clear_pending(db, provider.id, kind="xtream")
    logger.info(f"[Relink] {provider.name} before the sync: {counts['movies']} films and "
                f"{counts['episodes']} episodes repointed to its current address")
    return counts


def m3u_pending(db: Session, provider: Provider) -> bool:
    """An M3U provider's playlist moved and no full sync has followed it yet:
    the sync's client then rewrites every listed title's file that differs
    from the playlist (client.repoint_all), until a full sync read every
    category (finish_m3u_pending). Without one, M3U files stay as they are
    (tokens that rotate must not rewrite the library every night)."""
    if (provider.provider_type or "xtream") not in M3U_TYPES:
        return False
    entry = pending(db).get(str(provider.id))
    if not (isinstance(entry, dict) and entry.get("m3u")):
        return False
    logger.info(f"[Relink] {provider.name}: its playlist moved; this sync rewrites every listed "
                f"film and episode that plays another address")
    return True


def finish_m3u_pending(db: Session, provider: Provider, client) -> None:
    """After a full sync of an M3U provider that read every category."""
    if getattr(client, "repoint_all", False):
        clear_pending(db, provider.id, kind="m3u")


def resume_pending() -> None:
    """At start-up: the rewrites a restart cut short (the save's thread never
    ran or never finished), each run as _run does."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        todo = [(int(k), tuple(v)) for k, v in pending(db).items()
                if str(k).isdigit() and isinstance(v, list) and len(v) == 3]
    finally:
        db.close()
    for provider_id, before in todo:
        logger.info(f"[Relink] Resuming the .strm rewrite a restart cut short (provider {provider_id})")
        _run(provider_id, before)
