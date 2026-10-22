"""Forget people whose Jellyfin account was deleted.

Jellyfin's DELETE /Users/{id} signs the person out everywhere and removes the
playlists they own (Jellyfin.Api UserController.DeleteUser, 10.11). Tentacle's
side stayed: their row, playlist settings, home layout, requests,
notifications, music imports, Live TV lists and files, and the nightly kept
rebuilding playlists for an account that no longer exists. People is the
admin's view of Jellyfin's accounts, so whoever Jellyfin no longer has is
forgotten when People is opened and in the nightly sync.

Never forgotten: the owner (the first Tentacle user) and the account
Tentacle reads Jellyfin as (`jellyfin_user_id`), and nobody at all when
Jellyfin's list is empty or unreadable.
"""
import logging
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from models.database import (
    AutoPlaylistToggle, DownloadRequest, HomeRowOrder, ListSubscription, LiveUserChannel,
    LiveUserLineup, MediaRequest, MusicImport, Notification, TagRule, TentacleUser,
    get_setting, log_activity,
)
from services import livetv_lineups as L

logger = logging.getLogger(__name__)

HOME_CONFIG_DIR = Path("/data/home-configs")
PER_PERSON = (TagRule, HomeRowOrder, AutoPlaylistToggle, Notification, DownloadRequest,
              MediaRequest, MusicImport)


def gone(db: Session, jf_users: list) -> list:
    """The Tentacle rows whose Jellyfin account is gone."""
    present = {L.norm_user_id(u.get("Id") or "") for u in (jf_users or [])}
    present.discard("")
    if not present:
        return []
    owner = db.query(TentacleUser).order_by(TentacleUser.id).first()
    reader = L.norm_user_id(get_setting(db, "jellyfin_user_id", "") or "")
    return [r for r in db.query(TentacleUser).order_by(TentacleUser.id).all()
            if r.jellyfin_user_id not in present
            and r.id != owner.id and r.jellyfin_user_id != reader]


def forget_gone(db: Session, jf_users: list) -> list:
    """Forget everyone Jellyfin no longer has. Returns their names."""
    names = []
    for row in gone(db, jf_users):
        try:
            names.append(forget(db, row))
        except Exception as e:
            db.rollback()
            logger.warning(f"[People] Could not forget {row.display_name} ({row.jellyfin_user_id}): {e}")
    if names:
        from services import live_updates
        live_updates.changed("people", admins=True)
        live_updates.changed("requests", admins=True, clients=True)
    return names


def forget(db: Session, row: TentacleUser) -> str:
    """Remove one person's Tentacle data: rows, files, caches, the defaults
    that named them."""
    from services import family as F
    from services import smartlists as S

    uid, name, pk = row.jellyfin_user_id, row.display_name, row.id
    for sub in db.query(ListSubscription).filter(ListSubscription.user_id == pk).all():
        db.delete(sub)     # its cached list items go with it
    for model in PER_PERSON:
        db.query(model).filter(model.user_id == pk).delete(synchronize_session=False)
    for model in (LiveUserLineup, LiveUserChannel):
        db.query(model).filter(model.jellyfin_user_id == uid).delete(synchronize_session=False)
    db.delete(row)
    db.commit()

    d = F.defaults(db)
    if pk in (d["home_from"], d["channels_from"]):
        F.save_defaults(db, None if d["home_from"] == pk else d["home_from"],
                        None if d["channels_from"] == pk else d["channels_from"])

    smartlists = Path(get_setting(db, "smartlists_path", "/data/smartlists")) / uid
    if len(uid) == 32 and smartlists.is_dir():
        shutil.rmtree(smartlists, ignore_errors=True)
    home = HOME_CONFIG_DIR / f"{uid}.json"
    home.unlink(missing_ok=True)
    if len(uid) == 32 and (HOME_CONFIG_DIR / "backups").is_dir():
        for b in (HOME_CONFIG_DIR / "backups").glob(f"{uid}-*.json"):
            b.unlink(missing_ok=True)

    F.forget_visible(uid)
    with S._home_version_lock:
        S._home_versions.pop(pk, None)
    try:
        from routers import auth
        auth._token_profiles.pop(uid, None)
    except Exception:
        pass

    logger.info(f"[People] {name} ({uid}) is no longer in Jellyfin: forgot their Tentacle data")
    log_activity(db, "person_removed", f"{name} was removed from Jellyfin, so Tentacle forgot them too",
                 {"jellyfin_user_id": uid})
    return name
