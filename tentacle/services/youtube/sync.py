"""Periodic refresh: index every enabled channel, write files, apply retention."""
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from models.database import YouTubeChannel, YouTubeVideo, get_setting
from services.youtube import indexer, library
from services.youtube.errors import YouTubeError

logger = logging.getLogger(__name__)

# How long to let Jellyfin index newly written files before querying for their
# tags. Jellyfin reads NFO tags at scan time, so querying too early returns
# nothing and the playlist is built empty.
# How long to give Jellyfin to scan in the new files before the playlist is
# filled. A fixed 20-second sleep was here: against a full scan of a large
# library that takes minutes, the playlist was filled before the videos
# existed in Jellyfin, came up empty, and stayed empty — the library then
# filled in on its own and the row never appeared. The wait is now for the
# channel's own videos to show up, polled, with this as the ceiling.
# How long publishing waits in the foreground for Jellyfin to show the videos
# before handing off. Kept short on purpose: a Radarr add does not block on
# Jellyfin either. Whatever has not landed by then is topped up in the
# background as it arrives.
SCAN_MAX_WAIT_SECONDS = 30
SCAN_POLL_SECONDS = 3
# The background top-up: seconds between looks, quick at first while Jellyfin
# is importing, then sparser, about fifteen minutes in all. The hourly sync
# covers anything beyond that.
REFILL_SCHEDULE = (3, 3, 3, 3, 3, 3, 3, 3, 3, 3, 10, 10, 10, 10, 10, 10, 30, 30, 30, 30, 120, 600)


def base_url(db: Session) -> str:
    """The address written into .strm files.

    Must be reachable by the Jellyfin server, not just by a browser — Jellyfin's
    ffmpeg is what fetches it.
    """
    configured = (get_setting(db, "youtube_base_url", "") or "").strip()
    return configured.rstrip("/")


def _probe(url):
    """One GET, redirects not followed. A seam so this is testable offline."""
    import httpx
    with httpx.Client(timeout=10, follow_redirects=False) as c:
        return c.get(url)


def check_base_url(base: str) -> dict:
    """Fetch the address a .strm will carry and say whether it serves Tentacle.

    The setting only ever said the Jellyfin server must be able to reach it,
    which is true but unverified — and the failure is silent and badly
    misleading. An address behind Cloudflare Access, a reverse proxy asking for
    a login, or simply the wrong host answers a media request with an HTML login
    page, and ffmpeg reports that as "Invalid data found when processing input".
    Nothing in Tentacle or Jellyfin points at the address.

    The probe is deliberately an unauthenticated endpoint, because that is what
    a .strm is: checking an admin route instead would report every correctly
    configured instance as needing a login, ffmpeg having no session either.
    """
    try:
        r = _probe(f"{base.rstrip('/')}/api/youtube/ping")
    except Exception as e:
        return {"ok": False, "detail": f"Could not reach {base}: {e}"}

    if r.status_code in (301, 302, 303, 307, 308):
        where = r.headers.get("location", "")
        if "cloudflareaccess.com" in where or "/cdn-cgi/access/" in where:
            return {"ok": False, "detail":
                    "Cloudflare Access is protecting this address — it answers with a "
                    "login page, not media. Use the address on your own network "
                    "instead (for example http://192.168.1.10:8888)."}
        return {"ok": False, "detail":
                f"This address redirects to {where or 'somewhere else'}. A .strm has to "
                f"be served directly, with no login in front of it."}
    if r.status_code in (401, 403):
        return {"ok": False, "detail":
                f"This address asks for authentication (HTTP {r.status_code}). Jellyfin's "
                f"ffmpeg cannot log in — use an address with no login in front of it."}
    if r.status_code != 200:
        return {"ok": False, "detail": f"This address answered HTTP {r.status_code}."}
    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "detail":
                "This address answered with something other than Tentacle. Check it "
                "points at Tentacle itself and not a proxy or another service."}
    if not body.get("tentacle"):
        return {"ok": False, "detail": "This address is answering, but it is not Tentacle."}
    return {"ok": True, "detail": f"{base} serves Tentacle directly."}


def sync_channel(db: Session, channel: YouTubeChannel, base: str, on_progress=None) -> dict:
    """Index one channel, write any new media files, then apply retention."""
    result = indexer.index_channel(db, channel, on_progress=on_progress)
    if result.get("skipped"):
        return result

    written, art = 0, 0
    for video in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
    ).all():
        if not indexer.is_library_item(video):
            continue
        if video.strm_path:
            # Already written. Artwork arrived later than the writer did, so
            # videos indexed before it exist with no images — pick those up
            # instead of leaving them blank until something rewrites them.
            if video.folder_path:
                art += library.fetch_artwork(video, Path(video.folder_path))
            continue
        try:
            art += library.write_video(video, channel, base).get("artwork", 0)
            written += 1
        except OSError as e:
            logger.warning(f"[YouTube] Could not write files for {video.video_id}: {e}")
    db.commit()

    # Guide entries for live/upcoming streams when the channel is on Live TV.
    guide = 0
    if channel.live_enabled:
        from services.youtube import livetv
        guide = livetv.refresh_guide(db, channel)

    removed = apply_retention(db, channel)
    result.update({"written": written, "retired": removed, "guide": guide,
                   "artwork": art})
    return result


def apply_retention(db: Session, channel: YouTubeChannel) -> int:
    """Retire videos past the channel's keep window.

    Only ever acts on a listing that succeeded — index_channel raises otherwise,
    so this is never reached with a partial picture. Each removal touches a
    single video's own folder.
    """
    # Library items only. A live or upcoming stream is a guide entry, not one
    # of the "newest N videos", and counting it would push a real upload out.
    videos = db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        indexer.is_library_status(YouTubeVideo.live_status),
    ).order_by(YouTubeVideo.published_at.desc().nullslast()).all()

    doomed = []
    if channel.keep_count and len(videos) > channel.keep_count:
        doomed.extend(videos[channel.keep_count:])
    if channel.keep_days:
        cutoff = datetime.utcnow() - timedelta(days=channel.keep_days)
        doomed.extend(v for v in videos
                      if v.published_at and v.published_at < cutoff and v not in doomed)

    for video in doomed:
        library.remove_video(video)
        video.removed_at = datetime.utcnow()
        video.strm_path = None
    if doomed:
        db.commit()
        logger.info(f"[YouTube] Retired {len(doomed)} video(s) from '{channel.title}'")
    return len(doomed)


def _library_count(db: Session, channel: YouTubeChannel) -> int:
    """How many of this channel's videos are library items (not live)."""
    return db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        indexer.is_library_status(YouTubeVideo.live_status),
    ).count()


def youtube_library(jf) -> tuple:
    """(library id, the folder as Jellyfin sees it) for the YouTube library.

    Matched on the library's path or name. The path is what makes the fast
    import possible: Tentacle writes under its own mount, Jellyfin reads the
    same folder under whatever path it was given, and a "this path appeared"
    notice has to use Jellyfin's. (None, None) means it could not be told
    apart, in which case a scan is the only option left.
    """
    try:
        for lib in jf.get_libraries() or []:
            locations = [str(loc) for loc in (lib.get("Locations") or [])]
            hit = next((loc for loc in locations if loc.lower().rstrip("/").endswith("youtube")), None)
            if hit is None and "youtube" in str(lib.get("Name") or "").lower() and locations:
                hit = locations[0]
            if hit is not None:
                return lib.get("ItemId") or lib.get("Id"), hit
    except Exception as e:
        logger.debug(f"[YouTube] Could not list Jellyfin libraries: {e}")
    return None, None


def youtube_library_id(jf):
    return youtube_library(jf)[0]


def _jellyfin_paths(db: Session, channel: YouTubeChannel, jellyfin_root: str) -> list:
    """This channel's video folders, translated to Jellyfin's view of the mount."""
    from pathlib import PurePosixPath
    root = library.YOUTUBE_MEDIA_ROOT
    out = []
    for video in db.query(YouTubeVideo).filter(
        YouTubeVideo.channel_fk == channel.id,
        YouTubeVideo.removed_at.is_(None),
        YouTubeVideo.folder_path.isnot(None),
    ).all():
        try:
            rel = PurePosixPath(video.folder_path).relative_to(PurePosixPath(str(root)))
        except ValueError:
            continue
        folder = PurePosixPath(jellyfin_root.rstrip("/")) / rel
        out.append(str(folder))
        if video.strm_path:
            out.append(str(folder / PurePosixPath(video.strm_path).name))
    return out


def _wait_for_channel_items(jf, channel: YouTubeChannel, expected: int,
                            max_wait: int = None, on_stage=None) -> int:
    """Poll until Jellyfin has the channel's videos. Returns how many it has.

    Asks for exactly the items a playlist refresh will ask for — this
    channel's tag — so "the scan is done" means done for this channel, not
    for the whole server. The ceiling is read at call time, not bound as a
    default, so it can be adjusted without a restart.
    """
    if max_wait is None:
        max_wait = SCAN_MAX_WAIT_SECONDS
    deadline = time.monotonic() + max_wait
    last = -1
    while True:
        try:
            have = len(jf.query_items(include_types=["Movie"], tags=[f"yt:{channel.slug}"]) or [])
        except Exception as e:
            logger.debug(f"[YouTube] Count query failed for '{channel.title}': {e}")
            have = 0
        if have >= expected:
            return have
        if have != last:
            logger.info(f"[YouTube] Jellyfin has {have}/{expected} of '{channel.title}' — waiting")
            if on_stage:
                on_stage(f"waiting for Jellyfin to see {channel.title} ({have} of {expected})")
            last = have
        if time.monotonic() >= deadline:
            return have
        time.sleep(SCAN_POLL_SECONDS)


WARM_WORKERS = 4


def _warm_streams(db: Session, channels: list, on_stage=None) -> int:
    """Resolve each channel's library videos ahead of Jellyfin's probes.

    Skips anything already cached, so a re-publish costs nothing. Stops at the
    first bot check: warming is an optimisation, and hammering on through a
    block would turn it into an outage. Returns how many were resolved.
    """
    from concurrent.futures import ThreadPoolExecutor
    from services.youtube import resolver
    from services.youtube.errors import YouTubeBlocked

    todo = []
    for ch in channels:
        for video in db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == ch.id,
            YouTubeVideo.removed_at.is_(None),
            indexer.is_library_status(YouTubeVideo.live_status),
        ).all():
            if not resolver.is_cached(video.video_id):
                todo.append((video.video_id, ch.max_height or 1080))
    if not todo:
        return 0
    if on_stage:
        on_stage(f"preparing {len(todo)} stream(s) so Jellyfin imports quickly")

    blocked = threading.Event()
    done = 0

    def _one(item):
        vid, height = item
        if blocked.is_set():
            return False
        try:
            resolver.resolve(vid, height)
            return True
        except YouTubeBlocked as e:
            logger.warning(f"[YouTube] Bot-checked while preparing streams — stopping: {e}")
            blocked.set()
        except Exception as e:
            logger.debug(f"[YouTube] Could not prepare {vid}: {e}")
        return False

    with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
        done = sum(1 for ok in pool.map(_one, todo) if ok)
    logger.info(f"[YouTube] Prepared {done} of {len(todo)} stream(s) for Jellyfin's import")
    return done


def publish_to_jellyfin(db: Session, channels: list, on_stage=None) -> None:
    """Get the channels' videos into Jellyfin and their playlists filled.

    Without this nothing appears until Jellyfin's own scheduled scan: Tentacle
    writes the .strm and NFO, but Jellyfin reads the tags at scan time, so the
    playlists stay empty and their home rows are filtered out as empty — which
    looks exactly like the feature having done nothing.

    Done the way a Radarr download reaches Jellyfin, which is why that is
    fast: Jellyfin is told which folders just appeared and imports only
    those, in seconds. No library is scanned. A scan is the fallback for the
    one case it cannot work — the YouTube library could not be told apart
    from the others, so the paths cannot be translated.

    Then wait, briefly, until Jellyfin actually reports the channel's videos,
    fill the playlists, and if any is still short, look again a couple of
    minutes later rather than block. A playlist filled before its videos
    exist stays empty, which is the failure this replaces.
    """
    from models.database import TentacleUser
    from services.jellyfin import JellyfinService

    url = get_setting(db, "jellyfin_url", "")
    key = get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        logger.info("[YouTube] Jellyfin is not configured — files written, nothing published")
        return
    if not channels:
        return

    jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
    library_id, jellyfin_root = youtube_library(jf)

    # Jellyfin probes every .strm it imports, and each probe reaches Tentacle's
    # resolver, which asks YouTube — seconds per video, and the reason ten
    # videos took minutes to land while a Radarr file lands at once. Resolve
    # them first, in parallel, so the probes hit a warm cache and take under a
    # second each.
    _warm_streams(db, channels, on_stage)

    if on_stage:
        on_stage("telling Jellyfin about the new videos")
    try:
        if jellyfin_root:
            paths = [p for ch in channels for p in _jellyfin_paths(db, ch, jellyfin_root)][:500]
            jf.notify_media_updated(paths)
            # The notice goes through Jellyfin's file monitor, which waits
            # about a minute before acting on changed paths (Radarr adds take
            # that long to appear too; nobody is watching a toast for those).
            # A direct refresh of the one library runs immediately and is
            # seconds for a folder this size — so do both, and whichever
            # lands first wins.
            if library_id:
                jf.trigger_library_scan(library_id)
            logger.info(f"[YouTube] Told Jellyfin about {len(paths)} folder(s) under {jellyfin_root}"
                        + (f" and asked it to scan library {library_id} now" if library_id else ""))
        else:
            # Cannot translate paths, so the slow way it is. Say which
            # libraries were seen so the fix (a library named or located
            # 'youtube') is obvious.
            names = [str(lib.get("Name")) for lib in (jf.get_libraries() or [])]
            logger.warning(f"[YouTube] No Jellyfin library looks like the YouTube one (saw {names}) — "
                           f"scanning every library instead, which is slow. Name the library "
                           f"'YouTube' or point it at a folder called youtube.")
            jf.trigger_library_scan(None)
    except Exception as e:
        logger.warning(f"[YouTube] Could not tell Jellyfin about the new videos: {e}")
        return

    expected = {ch.id: _library_count(db, ch) for ch in channels}
    short = False
    for ch in channels:
        if expected[ch.id]:
            have = _wait_for_channel_items(jf, ch, expected[ch.id], on_stage=on_stage)
            if have < expected[ch.id]:
                short = True
                logger.info(
                    f"[YouTube] Jellyfin has {have} of {expected[ch.id]} videos for '{ch.title}' "
                    f"after {SCAN_MAX_WAIT_SECONDS}s — filling the playlist with what is there; "
                    f"the rest is topped up in the background as it lands")

    if on_stage:
        on_stage("filling the playlists")
    names = [ch.title for ch in channels]
    try:
        from services.smartlists import (
            _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin,
            bump_playlist_version, refresh_smartlist_playlists, sync_smartlists,
            write_home_config,
        )
        users = db.query(TentacleUser).all()
        for user in users:
            # sync_smartlists is what creates a playlist that is newly desired;
            # refresh only fills ones that already exist.
            sync_smartlists(db, user_id=user.id)
            refresh_smartlist_playlists(db, user_id=user.id, only_names=names)
            write_home_config(db, user_id=user.id)
        bump_playlist_version()
        _notify_jellyfin_plugin(db)

        # Say so if a playlist is still short. Silence here is what made an
        # empty row a mystery.
        for user in users:
            ids = {p["name"]: p["playlist_id"]
                   for p in _get_smartlists_with_playlist_ids(db, user_id=user.id)}
            for ch in channels:
                pid = ids.get(ch.title)
                if not pid or not expected[ch.id]:
                    continue
                try:
                    have = len(jf.get_playlist_items(pid) or [])
                except Exception:
                    continue
                if have < expected[ch.id]:
                    short = True
                    logger.warning(f"[YouTube] Playlist '{ch.title}' for user {user.id} holds {have} of "
                                   f"{expected[ch.id]} videos after publishing")
                else:
                    logger.info(f"[YouTube] Playlist '{ch.title}' for user {user.id}: {have} video(s)")
    except Exception as e:
        logger.warning(f"[YouTube] Playlist rebuild after scan failed: {e}")

    if short:
        if on_stage:
            on_stage("Jellyfin is picking the videos up — the row fills in as they land")
        start_background_refill()
    return {"short": short}


_refill_lock = threading.Lock()
_refill_running = False


def start_background_refill() -> bool:
    """Keep topping up short playlists as Jellyfin imports, without blocking.

    One loop at a time: it checks every channel's playlist, so a second one
    would only duplicate the work. Returns False if one is already running.
    """
    global _refill_running
    with _refill_lock:
        if _refill_running:
            return False
        _refill_running = True
    threading.Thread(target=_background_refill, daemon=True, name="youtube-refill").start()
    return True


def _background_refill() -> None:
    global _refill_running
    from models.database import SessionLocal
    try:
        for delay in REFILL_SCHEDULE:
            time.sleep(delay)
            db = SessionLocal()
            try:
                fixed, behind = reconcile_playlists(db, report=True)
            except Exception as e:
                logger.warning(f"[YouTube] Background top-up failed: {e}")
                continue
            finally:
                db.close()
            if fixed:
                logger.info(f"[YouTube] Topped up {fixed} playlist(s) as videos landed")
            if not behind:
                logger.info("[YouTube] Every channel playlist matches its library")
                return
        logger.warning("[YouTube] Some channel playlists are still behind their library after the "
                       "background top-ups; the hourly sync keeps checking. If this persists, Jellyfin "
                       "may not be scanning the YouTube folder — check the library's path and that "
                       "real-time monitoring is on.")
    finally:
        with _refill_lock:
            _refill_running = False


def reconcile_playlists(db: Session, report: bool = False):
    """Refill any channel playlist that holds fewer videos than the library.

    The safety net under publish_to_jellyfin: however a playlist came to be
    behind — an import still in progress, a Jellyfin restart mid-way — the
    next look catches it up without anyone noticing it was ever wrong.
    Returns how many playlists were refreshed; with report=True, a tuple of
    (refreshed, still_behind) so a caller can tell when there is nothing left.
    """
    from models.database import TentacleUser
    from services.jellyfin import JellyfinService
    from services.smartlists import (
        _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin,
        bump_playlist_version, refresh_smartlist_playlists, sync_smartlists,
    )

    url = get_setting(db, "jellyfin_url", "")
    key = get_setting(db, "jellyfin_api_key", "")
    if not (url and key):
        return (0, False) if report else 0
    jf = JellyfinService(url, key, get_setting(db, "jellyfin_user_id", ""))
    want = {ch.title: _library_count(db, ch)
            for ch in db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True).all()}  # noqa: E712
    fixed, still_behind = 0, False
    for user in db.query(TentacleUser).all():
        have_playlists = _get_smartlists_with_playlist_ids(db, user_id=user.id)
        # A user with no playlist for an enabled channel at all — created after
        # the channel was published, most likely — gets one made first.
        # Refilling can only top up a playlist that exists.
        if any(title not in {p["name"] for p in have_playlists} for title in want):
            try:
                sync_smartlists(db, user_id=user.id)
                have_playlists = _get_smartlists_with_playlist_ids(db, user_id=user.id)
                logger.info(f"[YouTube] Created missing channel playlist(s) for user {user.id}")
            except Exception as e:
                logger.warning(f"[YouTube] Could not create playlists for user {user.id}: {e}")
        short = []
        for p in have_playlists:
            if not p.get("is_youtube") or not want.get(p["name"]):
                continue
            try:
                have = len(jf.get_playlist_items(p["playlist_id"]) or [])
            except Exception:
                continue
            if have < want[p["name"]]:
                short.append((p["name"], p["playlist_id"], have, want[p["name"]]))
        if not short:
            continue
        names = [n for n, _, _, _ in short]
        # Only worth a refresh if Jellyfin has more than the playlist does;
        # otherwise the videos have not landed yet and there is nothing to add.
        can_grow = []
        for name, pid, have, want_n in short:
            try:
                slug = next(ch.slug for ch in db.query(YouTubeChannel).all() if ch.title == name)
                in_jf = len(jf.query_items(include_types=["Movie"], tags=[f"yt:{slug}"]) or [])
            except Exception:
                in_jf = 0
            if in_jf > have:
                can_grow.append(name)
            if max(in_jf, have) < want_n:
                still_behind = True
        if can_grow:
            logger.info(f"[YouTube] Playlist(s) behind the library for user {user.id}: {can_grow} — refilling")
            try:
                refresh_smartlist_playlists(db, user_id=user.id, only_names=can_grow)
                fixed += len(can_grow)
            except Exception as e:
                logger.warning(f"[YouTube] Refill failed for user {user.id}: {e}")
    if fixed:
        bump_playlist_version()
        _notify_jellyfin_plugin(db)
    return (fixed, still_behind) if report else fixed


def run_youtube_sync() -> dict:
    """Scheduler entry point."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        if get_setting(db, "youtube_enabled", "false") != "true":
            return {"enabled": False}
        base = base_url(db)
        if not base:
            logger.warning("[YouTube] youtube_base_url is not set — skipping (a .strm needs an address Jellyfin can reach)")
            return {"enabled": True, "error": "youtube_base_url not set"}

        totals = {"channels": 0, "new": 0, "written": 0, "retired": 0, "errors": 0}
        changed = []
        for channel in db.query(YouTubeChannel).filter(YouTubeChannel.enabled == True).all():  # noqa: E712
            totals["channels"] += 1
            try:
                r = sync_channel(db, channel, base)
                totals["new"] += r.get("new", 0)
                totals["written"] += r.get("written", 0)
                totals["retired"] += r.get("retired", 0)
                if r.get("written") or r.get("retired"):
                    changed.append(channel)
            except YouTubeError as e:
                totals["errors"] += 1
                logger.warning(f"[YouTube] '{channel.title}' failed: {e}")
        if changed:
            publish_to_jellyfin(db, changed)
        try:
            totals["refilled"] = reconcile_playlists(db)
        except Exception as e:
            logger.warning(f"[YouTube] Playlist check failed: {e}")
        if totals["new"] or totals["written"]:
            logger.info(f"[YouTube] Sync complete: {totals}")
        return totals
    finally:
        db.close()
