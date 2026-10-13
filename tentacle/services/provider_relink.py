"""A provider's new address or login reaches its VOD .strm files (#142).

A direct .strm is {server}/movie|series/{user}/{pass}/{id}.{ext}, written once
by the sync, and saving the provider changes only the provider row: every film
and episode already in the library kept the old, dead host. After a save that
changes the server or login, relink_strms rewrites this provider's own direct
files from the old form to the one the sync writes today. Anything else (a
Tentacle VOD link, which reads the provider's address when played, a proxy, a
hand-made file, another provider's rows) is left alone.
"""
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


def relink_strms(db: Session, provider: Provider, before: tuple) -> dict:
    """Rewrite the provider's direct .strm files that still carry `before`
    ((server, user, pass) from before the save, as for
    rewrite_xtream_channel_urls). Returns {"movies": n, "episodes": n}."""
    from services.sync import XtreamClient, _write_strm, chown_path, vod_links_for

    counts = {"movies": 0, "episodes": 0}
    if (_login(provider) == before or not provider.server_url or not before[0]
            or (provider.provider_type or "xtream") != "xtream"):
        return counts
    old = _old_url_re(before)
    client = XtreamClient(provider)
    client.vod_links = vod_links_for(db, provider)

    def relink(strm: Path, key: str) -> None:
        try:
            m = old.match(strm.read_text(encoding="utf-8").strip())
            if m is None:
                return
            kind, stream_id, ext = m.group(1).lower(), m.group(2), m.group(3)
            url = (client.movie_stream_url(stream_id, ext) if kind == "movie"
                   else client.episode_stream_url(stream_id, ext))
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
            return
        counts = relink_strms(db, provider, before)
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
