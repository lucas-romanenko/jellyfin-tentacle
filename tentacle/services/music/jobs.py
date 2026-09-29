"""Background music jobs (run one at a time by services.music.worker):

* finish_request: after an album is added to Lidarr, wait for its releases,
  pin the original and start the search;
* reconcile: the daily pass that refreshes the library snapshot and checks
  every monitored album (a dry run: it changes nothing in Lidarr);
* handle_webhook: Lidarr's "artist added" and "album imported" events.

Anything that would change files on disk waits for the user (phase 3's
review page, or that category's "apply automatically" setting). Only a pin
that touches no files happens right away: an album nothing is downloaded for.
"""
import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone

from models.database import MusicAlbum, MusicArtist, get_setting, set_setting
from services.lidarr import LidarrError
from services.music import library, original as rule, settings as music_settings, worker
from services.musicbrainz import MusicBrainz, MusicBrainzError

logger = logging.getLogger(__name__)

# How long to wait for Lidarr to load a new album's releases (about a minute).
RELEASE_WAIT = (2, 3, 5, 5, 10, 10, 15, 15)

progress = {"running": False, "done": 0, "total": 0, "started": None, "trigger": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── Requests ─────────────────────────────────────────────────────────────

def _request_done(row: MusicAlbum) -> None:
    row.request_pending, row.request_choice = False, None


def finish_request(album_id: int, rgid: str, choice: dict = None, sleep=time.sleep, resumed: bool = False):
    """The worker job that completes a request: pin the original, then search.

    The album's row stays `request_pending` until this has pinned and searched (or
    handed the album to review), so a request whose job was lost is finished later
    (finish_pending_requests). `resumed`: such a later run, which leaves an album
    that has files since to the daily check (a pin then could change files)."""
    def job(db):
        client = library.lidarr_client(db)
        album = client.album(album_id)
        for delay in RELEASE_WAIT:
            if album.get("releases"):
                break
            sleep(delay)
            album = client.album(album_id)
        row = library.upsert_album(db, album)
        if resumed and int((album.get("statistics") or {}).get("trackFileCount") or 0):
            _request_done(row)
            db.commit()
            logger.info(f"[Request] album '{row.title}' has files already; left to the daily check")
            return
        if not album.get("releases"):
            row.verdict = {"state": "Waiting for Lidarr to load this album's releases; the daily check "
                                    "will pin it."}
            db.commit()
            worker.record_error(f"'{album.get('title')}': Lidarr hadn't loaded its releases after a minute")
            return
        if not album.get("monitored"):
            client.set_monitored([album_id], True)
            album = client.album(album_id)

        mb = MusicBrainz.from_settings(db)
        prefs = rule.Prefs.from_settings(db)
        target = rule.choose_for_request(album, mb.release_group_releases(rgid), prefs, choice)
        if isinstance(target, rule.Ambiguous):
            row.category = rule.REVIEW
            row.verdict = rule.Verdict(rule.REVIEW, target.message + " Nothing was searched for yet.",
                                       reason=target.reason, options=target.options,
                                       have=row.track_file_count).to_dict()
            row.checked_at = datetime.utcnow()
            _request_done(row)   # the review's pick pins and searches (resolve_review)
            db.commit()
            logger.info(f"[Request] album '{row.title}' needs review before searching: {target.message}")
            return
        client.pin_release(album, target["foreignReleaseId"])
        client.search_albums([album_id])
        logger.info(f"[Request] album '{row.title}': pinned {target.get('title')} "
                    f"({target.get('trackCount')} tracks, {target.get('format')}); search started")
        _request_done(row)
        db.commit()
        row, album = library.sync_album(db, client, album_id)
        library.check_album(db, row, album, mb, prefs)
    return job


def finish_pending_requests(db, errors: list = None) -> int:
    """Finish requests whose pin-and-search job never ran (Tentacle restarted, or the
    job failed). Runs at startup and at the start of every daily check."""
    worker.run_urgent_jobs()   # a request's own job, if still queued, goes first
    rows = db.query(MusicAlbum).filter(MusicAlbum.request_pending.is_(True)).all()
    done = 0
    for row in rows:
        title = f"{row.artist_name} - {row.title}"
        if not row.lidarr_album_id:
            _request_done(row)
            db.commit()
            continue
        try:
            finish_request(row.lidarr_album_id, row.mbid, row.request_choice, resumed=True)(db)
            done += 1
            logger.info(f"[Request] '{title}': finished a request that was left unfinished")
        except LidarrError as e:
            if e.status == 404:   # removed from Lidarr since: nothing is owed
                _request_done(row)
                db.commit()
            elif errors is not None:
                errors.append(f"{title}: {e.message}")
        except (MusicBrainzError, library.MusicUnavailable) as e:
            if errors is not None:
                errors.append(f"{title}: {e.message}")
    return done


def resume_requests() -> None:
    """At startup: queue the requests a restart interrupted (if any)."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        if not music_settings.is_enabled(db):
            return
        if db.query(MusicAlbum).filter(MusicAlbum.request_pending.is_(True)).count():
            worker.submit(lambda job_db: finish_pending_requests(job_db), worker.URGENT, "unfinished requests")
    except Exception as e:   # never block startup
        logger.warning(f"[Music] Couldn't look for unfinished requests: {e}")
    finally:
        db.close()


def resolve_review(album_id: int, rgid: str, choice: dict):
    """The user picked a tracklist for a review item: pin it and search if nothing is downloaded."""
    def job(db):
        client = library.lidarr_client(db)
        album = client.album(album_id)
        mb = MusicBrainz.from_settings(db)
        prefs = rule.Prefs.from_settings(db)
        target = rule.choose_for_request(album, mb.release_group_releases(rgid), prefs, choice)
        if isinstance(target, rule.Ambiguous):
            raise library.MusicUnavailable(target.message)
        client.pin_release(album, target["foreignReleaseId"])
        have = int((album.get("statistics") or {}).get("trackFileCount") or 0)
        if have < (target.get("trackCount") or 0):
            client.search_albums([album_id])
        row, album = library.sync_album(db, client, album_id)
        verdict = rule.Verdict(rule.RIGHT, f"Pinned to your choice: {target.get('trackCount')} tracks.",
                               target=rule.summarize(target), pinned=rule.summarize(target), have=have, locked=True)
        row.category, row.verdict, row.checked_at = rule.RIGHT, verdict.to_dict(), datetime.utcnow()
        db.commit()
        logger.info(f"[Music] '{row.title}': pinned the {target.get('trackCount')}-track release chosen in review")
    return job


# ── Reconcile ────────────────────────────────────────────────────────────

def reconcile(trigger: str = "daily"):
    def job(db):
        try:
            _reconcile(db, trigger)
        finally:
            progress["running"] = False
    return job


def _reconcile(db, trigger: str):
    if not music_settings.is_enabled(db):
        return
    client = library.lidarr_client(db)
    mb = MusicBrainz.from_settings(db)
    prefs = rule.Prefs.from_settings(db)
    counts, errors, checked = Counter(), [], 0
    finish_pending_requests(db, errors)
    artists = client.artists()
    progress.update(running=True, done=0, total=len(artists), started=_now(), trigger=trigger)
    for i, artist in enumerate(artists):
        worker.run_urgent_jobs()
        progress["done"] = i + 1
        try:
            pairs = library.sync_artist(db, client, artist)
        except LidarrError as e:
            errors.append(f"{artist.get('artistName')}: {e.message}")
            continue
        for row, album in pairs:
            if not album.get("monitored"):
                continue
            try:
                verdict = library.check_album(db, row, album, mb, prefs)
            except MusicBrainzError as e:
                errors.append(f"{row.artist_name} - {row.title}: {e.message}")
                continue
            counts[verdict.category] += 1
            checked += 1
    applied = _auto_apply(db, client, mb, prefs, errors)
    pictures = picture_pass(db)
    # Artists no longer in Lidarr's list (not ones whose read failed above).
    in_lidarr = {a.get("id") for a in artists} or {-1}
    for gone in db.query(MusicArtist).filter(MusicArtist.lidarr_artist_id.notin_(in_lidarr)).all():
        db.query(MusicAlbum).filter(MusicAlbum.lidarr_artist_id == gone.lidarr_artist_id).delete()
        db.delete(gone)
    db.commit()
    mb.cleanup_cache()
    summary = {"started": progress["started"], "finished": _now(), "trigger": trigger,
               "artists": len(artists), "albums_checked": checked, "counts": dict(counts),
               "applied": applied, "pictures": pictures,
               "errors": len(errors), "first_errors": errors[:5]}
    set_setting(db, "music_last_reconcile", json.dumps(summary))
    logger.info(f"[Music] Reconcile ({trigger}): {checked} albums checked"
                + "".join(f", {k} {v}" for k, v in sorted(counts.items()))
                + (f"; {len(errors)} errors" if errors else ""))
    if errors:
        worker.record_error(f"Reconcile: {len(errors)} albums couldn't be checked (first: {errors[0]})")


def start_reconcile(trigger: str = "daily") -> bool:
    """Queue a reconcile unless one is already running or queued."""
    if progress["running"]:
        return False
    progress.update(running=True, done=0, total=0, started=_now(), trigger=trigger)
    worker.submit(reconcile(trigger), worker.BACKGROUND, f"reconcile ({trigger})")
    return True


def scheduled_reconcile():
    """APScheduler entry point (daily, at the music_reconcile_time setting)."""
    from models.database import SessionLocal
    db = SessionLocal()
    try:
        enabled = music_settings.is_enabled(db)
        ready = enabled and get_setting(db, "lidarr_url") and get_setting(db, "lidarr_api_key")
        if ready:
            start_reconcile("daily")
        if enabled:
            # Queued behind the check, so Discover → Music is fresh by morning.
            from services.music import discover
            discover.ensure_fresh(db)
    finally:
        db.close()


# ── Webhook ──────────────────────────────────────────────────────────────

def handle_webhook(payload: dict):
    event = (payload or {}).get("eventType") or ""

    def job(db):
        client = library.lidarr_client(db)
        mb = MusicBrainz.from_settings(db)
        prefs = rule.Prefs.from_settings(db)
        artist = payload.get("artist") or {}
        if event == "ArtistAdd" and artist.get("id"):
            retry_pictures_later(artist["id"], delay=900)
            queue_ids, _ = library.queued(client)
            for row, album in library.sync_artist(db, client, client.artist(artist["id"])):
                if not album.get("monitored"):
                    continue
                verdict = library.check_album(db, row, album, mb, prefs)
                # Nothing downloaded yet: pinning the original changes no files.
                if verdict.category == rule.REPIN_DOWNLOAD and verdict.have == 0 and verdict.target:
                    client.pin_release(album, verdict.target["id"])
                    if album.get("id") not in queue_ids:
                        client.search_albums([album["id"]])
                    row, album = library.sync_album(db, client, album["id"])
                    library.check_album(db, row, album, mb, prefs)
                    logger.info(f"[Music webhook] '{row.artist_name} - {row.title}': pinned the original "
                                f"({verdict.target.get('tracks')} tracks)")
        elif event == "Download":
            album_ref = payload.get("album") or {}
            if album_ref.get("id"):
                row, album = library.sync_album(db, client, album_ref["id"])
                library.check_album(db, row, album, mb, prefs)
                logger.info(f"[Music webhook] '{row.artist_name} - {row.title}' imported: {row.category}")
            from services.music.players import rescan_all
            rescan_all(db)
            if artist.get("id"):
                retry_pictures_later(artist["id"])
        elif event == "ArtistDelete" and artist.get("id"):
            db.query(MusicAlbum).filter(MusicAlbum.lidarr_artist_id == artist["id"]).delete()
            db.query(MusicArtist).filter(MusicArtist.lidarr_artist_id == artist["id"]).delete()
            db.commit()
        elif event == "AlbumDelete":
            ref = payload.get("album") or {}
            if ref.get("id"):
                db.query(MusicAlbum).filter(MusicAlbum.lidarr_album_id == ref["id"]).delete()
                db.commit()
    return job, event


# ── Applying verdicts (phase 3) ──────────────────────────────────────────

AUTO_SETTINGS = {rule.REPIN: "music_auto_repin", rule.REPIN_TRIM: "music_auto_repin_trim",
                 rule.REPIN_DOWNLOAD: "music_auto_repin_download"}


def record_stop(db, row: MusicAlbum, message: str) -> None:
    """Applying stopped safely: the album goes to review with the reason."""
    verdict = dict(row.verdict or {})
    verdict.update(category=rule.REVIEW, reason="apply_stopped", message=message)
    row.category, row.verdict, row.checked_at = rule.REVIEW, verdict, datetime.utcnow()
    db.commit()


def _apply_one(db, client, mb, prefs, row: MusicAlbum, expected: str) -> str:
    from services.music.apply import ApplyStopped, apply_album
    try:
        verdict = apply_album(db, row, client, mb, prefs, expected=expected)
        return verdict.category
    except ApplyStopped as e:
        logger.warning(f"[Music] '{row.artist_name} - {row.title}': {e.message}")
        record_stop(db, row, e.message)
        return rule.REVIEW
    except (LidarrError, MusicBrainzError) as e:
        logger.warning(f"[Music] '{row.artist_name} - {row.title}': couldn't apply: {e.message}")
        record_stop(db, row, f"Couldn't apply: {e.message}")
        return rule.REVIEW


def _auto_apply(db, client, mb, prefs, errors: list) -> int:
    """Categories whose "apply automatically" is on (all off by default)."""
    on = [cat for cat, key in AUTO_SETTINGS.items() if (get_setting(db, key, "false") or "").lower() == "true"]
    if not on:
        return 0
    done = 0
    for row in db.query(MusicAlbum).filter(MusicAlbum.category.in_(on), MusicAlbum.monitored.is_(True)).all():
        worker.run_urgent_jobs()
        if _apply_one(db, client, mb, prefs, row, row.category) != rule.REVIEW:
            done += 1
        else:
            errors.append(f"{row.artist_name} - {row.title}: {(row.verdict or {}).get('message')}")
    return done


def apply_albums(mbids: list):
    """The review page's Apply (one album, or a whole group): one album at a time."""
    def job(db):
        client = library.lidarr_client(db)
        mb = MusicBrainz.from_settings(db)
        prefs = rule.Prefs.from_settings(db)
        for mbid in mbids:
            row = db.query(MusicAlbum).filter(MusicAlbum.mbid == mbid).first()
            if not row or not row.lidarr_album_id or row.category not in AUTO_SETTINGS:
                continue
            expected = row.category
            row.verdict = dict(row.verdict or {}, state="Applying…")
            db.commit()
            _apply_one(db, client, mb, prefs, row, expected)
            worker.run_urgent_jobs()
    return job


def lock_albums():
    def job(db):
        from services.music.apply import lock_right_albums
        n = lock_right_albums(db, library.lidarr_client(db))
        logger.info(f"[Music] Locked {n} albums to their pinned original release")
    return job


# ── Artist pictures (phase 3) ────────────────────────────────────────────

def picture_pass(db, statuses=("", "waiting", "error", "review")) -> dict:
    """Pictures for artists that don't have one settled yet. Returns counts."""
    from services.music.pictures import ensure_picture
    counts = Counter()
    for artist in db.query(MusicArtist).filter(MusicArtist.picture_status.in_(statuses)).all():
        worker.run_urgent_jobs()
        try:
            counts[ensure_picture(db, artist)] += 1
        except Exception as e:
            artist.picture_status, artist.picture_note = "error", str(e)
            db.commit()
            counts["error"] += 1
    return dict(counts)


def pictures_job(statuses=("", "waiting", "error", "review")):
    def job(db):
        counts = picture_pass(db, statuses)
        logger.info(f"[Music] Artist pictures: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to do"))
    return job


def picture_for(mbid: str, data: bytes = None, deezer_url: str = None):
    """The user uploaded a picture, or picked a Deezer candidate, for one artist."""
    def job(db):
        from services.music.pictures import download, ensure_picture, looks_like_placeholder
        artist = db.query(MusicArtist).filter(MusicArtist.mbid == mbid).first()
        if not artist:
            return
        image, source = data, "upload"
        if deezer_url:
            image, source = download(deezer_url), "upload"
        if not image or looks_like_placeholder(image):
            artist.picture_status, artist.picture_note = "review", "That picture is a placeholder, not a photo."
            db.commit()
            return
        ensure_picture(db, artist, data=image, source=source, force=True)
    return job


def retry_pictures_later(artist_id: int, delay: int = 300) -> None:
    """After an import, give the player time to scan the artist in, then set its picture."""
    def run():
        worker.submit(_pictures_for_lidarr_artist(artist_id), worker.NORMAL, "artist picture")
    try:
        from main import schedule_once
        schedule_once(run, delay, f"music_picture_{artist_id}")
    except Exception as e:  # no scheduler (tests, CLI): the daily check catches up
        logger.debug(f"[Music] couldn't schedule a picture retry: {e}")


def _pictures_for_lidarr_artist(artist_id: int):
    def job(db):
        from services.music.pictures import ensure_picture
        artist = db.query(MusicArtist).filter(MusicArtist.lidarr_artist_id == artist_id).first()
        if artist and artist.picture_status in ("", "waiting", "error"):
            ensure_picture(db, artist)
    return job
