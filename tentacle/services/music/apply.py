"""Applying a reconcile verdict to one album, through Lidarr.

* re-pin: pin the original release. Lidarr unlinks the album's files and
  rescans the artist folder itself (AlbumEditedService), re-matching them to
  the new release's tracks. Tentacle waits for that rescan and checks every
  original track found its file.
* re-pin + remove extra tracks: the same, then the album's files that no
  longer match any track of the pinned release are deleted, by Lidarr
  (DELETE /trackfile/{id}, so Lidarr's recycle bin applies). Only when exactly
  the expected number is left over and every original track matched:
  anything else stops with nothing deleted and the album goes to review.
* re-pin + download: pin, wait for the rescan when there are files, then
  search for what is missing.

Tentacle never touches the files itself. The verdict is re-computed from
fresh Lidarr and MusicBrainz data first; if it changed, nothing is applied.
"""
import logging
import time

from models.database import MusicAlbum, log_deletion
from services.lidarr import LidarrClient, LidarrError
from services.music import library, original as rule

logger = logging.getLogger(__name__)

RESCAN_TIMEOUT = 180
RESCAN_POLL = 3
APPLICABLE = (rule.REPIN, rule.REPIN_TRIM, rule.REPIN_DOWNLOAD)


class ApplyStopped(Exception):
    """Applying stopped safely (nothing more was changed). `message` is for the user."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _is_rescan(command: dict) -> bool:
    return (command.get("name") or command.get("commandName") or "").replace(" ", "").lower() == "rescanfolders"


def command_ids(client: LidarrClient) -> set:
    return {c.get("id") for c in client.commands()}


def wait_for_rescan(client: LidarrClient, known_ids: set, sleep=time.sleep, clock=time.monotonic,
                    timeout: int = RESCAN_TIMEOUT) -> None:
    """Wait until the rescan Lidarr queued after the pin has finished.

    Lidarr's command ids only increase, so "a RescanFolders whose id wasn't
    there before the pin" is ours, whatever the two machines' clocks say.
    """
    deadline = clock() + timeout
    while True:
        ours = [c for c in client.commands() if _is_rescan(c) and c.get("id") not in known_ids]
        states = {(c.get("status") or "").lower() for c in ours}
        if states & {"failed", "aborted", "cancelled", "orphaned"}:
            raise ApplyStopped("Lidarr's rescan of the artist folder failed; nothing was deleted.")
        if ours and not states & {"queued", "started"}:
            return
        if clock() >= deadline:
            raise ApplyStopped("Lidarr was still rescanning the artist folder after "
                               f"{timeout // 60} minutes; nothing was deleted. Try again later.")
        sleep(RESCAN_POLL)


def apply_album(db, row: MusicAlbum, client: LidarrClient, mb, prefs: rule.Prefs,
                expected: str = None, sleep=time.sleep, clock=time.monotonic) -> rule.Verdict:
    """Apply the album's verdict. Returns the verdict after applying."""
    album = client.album(row.lidarr_album_id)
    verdict = rule.evaluate(album, mb.release_group_releases(row.mbid), prefs)
    if verdict.category not in APPLICABLE:
        library.check_album(db, row, album, mb, prefs)
        raise ApplyStopped(f"Nothing to apply: the album is now '{verdict.category}'.")
    if expected and verdict.category != expected:
        library.check_album(db, row, album, mb, prefs)
        raise ApplyStopped(f"The verdict changed from '{expected}' to '{verdict.category}' since the "
                           f"last check; nothing was applied. Check it again.")

    target, have = verdict.target, verdict.have
    before = client.trackfiles(row.lidarr_album_id) if have else []
    known = command_ids(client) if have else set()
    client.pin_release(album, target["id"])
    logger.info(f"[Music] '{row.artist_name} - {row.title}': pinned {target.get('title')} "
                f"({target.get('tracks')} tracks, {target.get('format')})")

    if have:
        wait_for_rescan(client, known, sleep=sleep, clock=clock)
        matched = client.trackfiles(row.lidarr_album_id)
        wanted = min(target.get("tracks") or 0, have)
        if len(matched) < wanted and verdict.category != rule.REPIN_DOWNLOAD:
            raise ApplyStopped(f"After re-pinning, Lidarr matched {len(matched)} of your files to the "
                               f"{target.get('tracks')} tracks; nothing was deleted. Check the album in Lidarr.")
        if verdict.category == rule.REPIN_TRIM:
            _remove_extra(db, row, client, before, matched, have - (target.get("tracks") or 0))

    if verdict.category == rule.REPIN_DOWNLOAD:
        client.search_albums([row.lidarr_album_id])

    row, album = library.sync_album(db, client, row.lidarr_album_id)
    return library.check_album(db, row, album, mb, prefs)


def _remove_extra(db, row, client: LidarrClient, before: list, matched: list, expected: int):
    matched_paths = {f.get("path") for f in matched}
    matched_ids = {f.get("id") for f in matched}
    leftover_ids = [f["id"] for f in before if f.get("id") not in matched_ids and f.get("path") not in matched_paths]
    leftovers = [f for f in client.trackfiles_by_id(leftover_ids) if f.get("path") not in matched_paths]
    if len(leftovers) != expected:
        raise ApplyStopped(f"Expected {expected} extra tracks after re-pinning but found {len(leftovers)}; "
                           f"nothing was deleted. Check the album in Lidarr.")
    for f in leftovers:
        client.delete_trackfile(f["id"])
        log_deletion(db, kind="music-extra-track", name=f"{row.artist_name} - {row.title}", media_type="music",
                     size_bytes=f.get("size"), reason="re-pinned to the original release",
                     detail=f"{f.get('path')} (removed through Lidarr)")
    logger.info(f"[Music] '{row.artist_name} - {row.title}': removed {len(leftovers)} extra tracks through Lidarr")


def lock_right_albums(db, client: LidarrClient) -> int:
    """Turn "any release OK" off for albums already pinned right. Changes no files:
    Lidarr only rescans when it is turned ON, not off."""
    rows = db.query(MusicAlbum).filter(MusicAlbum.category == rule.RIGHT, MusicAlbum.monitored.is_(True),
                                       MusicAlbum.any_release_ok.is_(True)).all()
    locked = 0
    for row in rows:
        album = client.album(row.lidarr_album_id)
        pinned = next((r for r in album.get("releases") or [] if r.get("monitored")), None)
        if not pinned or not album.get("anyReleaseOk", True):
            continue
        client.pin_release(album, pinned["foreignReleaseId"])
        row.any_release_ok = False
        verdict = dict(row.verdict or {}, locked=True)
        row.verdict = verdict
        db.commit()
        locked += 1
    return locked
