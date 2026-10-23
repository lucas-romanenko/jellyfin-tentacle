"""YouTube channels per person (Oct 2026).

Everyone adds their own channels (My YouTube); an admin can add one for
someone, which is how a kid gets theirs. A channel is fetched once, however
many people follow it, and when its last follower leaves it is removed with
its files (routers/youtube.remove_channel, the same removal as before).

Who sees what is Jellyfin's BlockedTags, as for Live TV places and kids
(services/livetv_tuners.py): each channel has two tags,
  - `yt:<slug>` on its videos (in every NFO since the start), and
  - `tentacle-youtube-<id>` on its Live TV channel and the channel's
    programmes (the plugin sets it when Jellyfin creates the channel, from
    the stream address; the Live TV sync after each guide refresh),
and everyone blocks both tags of every channel they don't follow. Their own
blocked tags are kept. Tentacle reads YouTube items without a user, so an
admin who doesn't follow a channel never hides it from Tentacle itself.
"""
import logging
import threading
from typing import Optional

import requests
from sqlalchemy.orm import Session

from models.database import TentacleUser, YouTubeChannel, YouTubeFollow

logger = logging.getLogger(__name__)

VIDEO_TAG_PREFIX = "yt:"
LIVE_TAG_PREFIX = "tentacle-youtube-"

# Policies are read, changed and written back; two writers at once would
# lose one change. The Live TV sync takes it too (livetv_tuners.apply_policies).
policy_lock = threading.RLock()


def norm(user_id: str) -> str:
    return (user_id or "").replace("-", "").lower()


def video_tag(channel: YouTubeChannel) -> str:
    return f"{VIDEO_TAG_PREFIX}{channel.slug}"


def live_tag(channel_id: int) -> str:
    return f"{LIVE_TAG_PREFIX}{channel_id}"


def is_ours(tag) -> bool:
    t = str(tag).lower()
    return t.startswith(VIDEO_TAG_PREFIX) or t.startswith(LIVE_TAG_PREFIX)


# ── Who follows what ────────────────────────────────────────────────────────

def follower_ids(db: Session, channel_id: int) -> set:
    return {r.jellyfin_user_id for r in
            db.query(YouTubeFollow).filter(YouTubeFollow.channel_fk == channel_id).all()}


def followed_ids(db: Session, user_id: str) -> set:
    """The ids of the channels this person follows."""
    return {r.channel_fk for r in
            db.query(YouTubeFollow).filter(YouTubeFollow.jellyfin_user_id == norm(user_id)).all()}


def followed_channels(db: Session, user_id: str) -> list:
    return (db.query(YouTubeChannel)
            .join(YouTubeFollow, YouTubeFollow.channel_fk == YouTubeChannel.id)
            .filter(YouTubeFollow.jellyfin_user_id == norm(user_id))
            .order_by(YouTubeChannel.title).all())


def followers_by_channel(db: Session) -> dict:
    """channel id -> set of follower ids."""
    out = {}
    for r in db.query(YouTubeFollow).all():
        out.setdefault(r.channel_fk, set()).add(r.jellyfin_user_id)
    return out


def kids_channel_ids(db: Session) -> set:
    """Channels a kids profile follows: their Live TV channels are kids
    channels (other kids still block them unless they follow them too)."""
    kids = {t.jellyfin_user_id for t in
            db.query(TentacleUser).filter(TentacleUser.kids_level.isnot(None)).all()}
    if not kids:
        return set()
    return {r.channel_fk for r in
            db.query(YouTubeFollow).filter(YouTubeFollow.jellyfin_user_id.in_(kids)).all()}


def follow(db: Session, channel: YouTubeChannel, user_id: str, added_by: Optional[str] = None) -> bool:
    """Returns True when it is new."""
    uid = norm(user_id)
    if db.query(YouTubeFollow).filter(YouTubeFollow.channel_fk == channel.id,
                                      YouTubeFollow.jellyfin_user_id == uid).first():
        return False
    db.add(YouTubeFollow(channel_fk=channel.id, jellyfin_user_id=uid, added_by=added_by))
    db.commit()
    return True


def unfollow(db: Session, channel_id: int, user_id: str) -> bool:
    """Returns True when they followed it."""
    n = db.query(YouTubeFollow).filter(YouTubeFollow.channel_fk == channel_id,
                                       YouTubeFollow.jellyfin_user_id == norm(user_id)).delete()
    db.commit()
    return bool(n)


# ── Blocked tags ────────────────────────────────────────────────────────────

def all_tags(db: Session) -> dict:
    """channel id -> its two tags."""
    return {c.id: (video_tag(c), live_tag(c.id)) for c in db.query(YouTubeChannel).all()}


def blocked_for(db: Session, user_id: str, tags: Optional[dict] = None,
                follows: Optional[dict] = None) -> list:
    """The YouTube tags this person blocks: those of every channel they
    don't follow."""
    uid = norm(user_id)
    tags = all_tags(db) if tags is None else tags
    follows = followers_by_channel(db) if follows is None else follows
    out = set()
    for cid, pair in tags.items():
        if uid not in follows.get(cid, ()):
            out.update(pair)
    return sorted(out)


def with_blocked(policy_tags, youtube_tags) -> list:
    """BlockedTags with Tentacle's YouTube tags set to `youtube_tags`, the
    rest kept in their order."""
    return [t for t in (policy_tags or []) if not is_ours(t)] + sorted(youtube_tags)


def _jellyfin(db):
    from services.livetv_tuners import jellyfin_for
    return jellyfin_for(db)


def apply_person(db: Session, user_id: str, jf=None) -> bool:
    """Set one person's YouTube blocked tags in Jellyfin now. Returns True
    when their policy changed. Never raises: the hourly pass catches up."""
    jf = jf or _jellyfin(db)
    if jf is None or not user_id:
        return False
    want_yt = blocked_for(db, user_id)
    try:
        with policy_lock:
            user = jf.get(f"/Users/{user_id}") or {}
            policy = user.get("Policy") or {}
            if not policy:
                return False
            want = with_blocked(policy.get("BlockedTags"), want_yt)
            if list(policy.get("BlockedTags") or []) == want:
                return False
            jf.post(f"/Users/{user['Id']}/Policy", {**policy, "BlockedTags": want})
        return True
    except requests.RequestException as e:
        logger.warning(f"[YouTube] Could not set who sees which channels for {user_id}: {e}")
        return False


def apply_everyone(db: Session, jf=None) -> int:
    """Every Jellyfin account's YouTube blocked tags (also people who never
    opened Tentacle: they follow nothing). Returns how many changed."""
    jf = jf or _jellyfin(db)
    if jf is None:
        return 0
    try:
        users = jf.get("/Users") or []
    except requests.RequestException as e:
        logger.warning(f"[YouTube] Could not read Jellyfin's people: {e}")
        return 0
    tag_studios(db, jf)
    tags, follows = all_tags(db), followers_by_channel(db)
    changed = 0
    for u in users:
        want_yt = blocked_for(db, u.get("Id"), tags, follows)
        if with_blocked((u.get("Policy") or {}).get("BlockedTags"), want_yt) == \
                list((u.get("Policy") or {}).get("BlockedTags") or []):
            continue
        if apply_person(db, u["Id"], jf):
            changed += 1
    if changed:
        logger.info(f"[YouTube] Updated who sees which channels for {changed} people")
    return changed


def tag_studios(db: Session, jf=None) -> int:
    """Give each channel's Studio item (Jellyfin makes one from the NFO's
    <studio>, the channel's name) the channel's video tag: search lists
    studios too, and an untagged one showed the channel's name to people
    who don't follow it. Returns how many changed; never raises."""
    jf = jf or _jellyfin(db)
    if jf is None:
        return 0
    from services.jellyfin import _item_update_payload
    changed = 0
    for ch in db.query(YouTubeChannel).all():
        try:
            found = (jf.get("/Studios", searchTerm=ch.title, enableImages="false") or {}).get("Items") or []
            for studio in (s for s in found if s.get("Name") == ch.title):
                item = ((jf.get("/Items", ids=studio["Id"], fields="Tags,Genres,Studios,People,ProviderIds,Overview",
                                enableUserData="false", enableImages="false") or {}).get("Items") or [None])[0]
                if not item or video_tag(ch) in (item.get("Tags") or []):
                    continue
                jf.post(f"/Items/{item['Id']}", _item_update_payload(item, Tags=list(item.get("Tags") or []) + [video_tag(ch)]))
                changed += 1
        except Exception as e:   # a Jellyfin without /Studios, an odd answer: search only
            logger.info(f"[YouTube] Studio of '{ch.title}' not tagged: {e}")
    if changed:
        logger.info(f"[YouTube] Tagged {changed} channel studio(s): only their followers find them")
    return changed


# A channel an admin adds for a kid with no rating gets this one: the parent
# vouched for it. Kids profiles hide unrated films (services/family.py), and
# every kids level allows G. Only kids are told anything by a rating, and
# other kids still don't see the channel unless it is theirs too.
KIDS_RATING = "G"
_ITEM_FIELDS = ("Tags,Genres,Studios,People,ProviderIds,Overview,OriginalTitle,Taglines,DateCreated,"
                "CustomRating,ProductionLocations,ForcedSortName,PremiereDate,ProductionYear")


def rate_for_kids(db: Session, channel: YouTubeChannel, jf=None) -> int:
    """Rate an unrated channel G for the kid it was added for: its NFOs (for
    a re-import) and its videos in Jellyfin, which doesn't re-read a locked
    NFO. Returns how many Jellyfin items changed; never raises."""
    if channel.rating:
        return 0
    channel.rating = KIDS_RATING
    db.commit()
    from models.database import YouTubeVideo
    from services.youtube import library
    from services.youtube.sync import base_url
    base = base_url(db)
    for video in db.query(YouTubeVideo).filter(YouTubeVideo.channel_fk == channel.id,
                                               YouTubeVideo.removed_at.is_(None)).all():
        library.rewrite_nfo(video, channel, base)
    jf = jf or _jellyfin(db)
    if jf is None:
        return 0
    from services.jellyfin import _item_update_payload
    changed = 0
    try:
        items = (jf.get("/Items", tags=video_tag(channel), includeItemTypes="Movie", recursive="true",
                        fields=_ITEM_FIELDS, enableUserData="false", enableImages="false") or {}).get("Items") or []
        for item in items:
            if item.get("OfficialRating") == KIDS_RATING:
                continue
            jf.post(f"/Items/{item['Id']}", _item_update_payload(item, OfficialRating=KIDS_RATING))
            changed += 1
    except requests.RequestException as e:
        logger.warning(f"[YouTube] '{channel.title}' not rated for kids in Jellyfin: {e}")
    logger.info(f"[YouTube] '{channel.title}' rated {KIDS_RATING}: added for a kid ({changed} video(s) in Jellyfin)")
    return changed


CLEANED_KEY = "youtube_follows_cleaned"


def drop_old_playlists(db: Session) -> int:
    """Once, after the migration (channels became their followers' only):
    everyone else's playlist, home row and hero of each channel go, as when
    they stop following it (routers/youtube._drop_for_people). Returns how
    many people were tidied."""
    from models.database import get_setting, set_setting
    if get_setting(db, CLEANED_KEY, "") or get_setting(db, "youtube_follows_migrated", "") != "true":
        return 0
    from routers.youtube import _cleanup_for_people
    follows = followers_by_channel(db)
    people = db.query(TentacleUser).all()
    tidied = set()
    for ch in db.query(YouTubeChannel).all():
        others = [u.id for u in people if norm(u.jellyfin_user_id) not in follows.get(ch.id, set())]
        if others:
            _cleanup_for_people(ch.title, ch.slug, others)
            tidied.update(others)
    set_setting(db, CLEANED_KEY, "true")
    db.commit()
    if tidied:
        logger.info(f"[YouTube] Took the YouTube playlists of channels they don't follow away from {len(tidied)} people")
    return len(tidied)


def apply_everyone_soon(reason: str = "") -> None:
    """apply_everyone in the background, with its own session."""
    def _run():
        from models.database import SessionLocal
        db = SessionLocal()
        try:
            apply_everyone(db)
        except Exception as e:
            logger.warning(f"[YouTube] Blocked tags not updated ({reason}): {e}")
        finally:
            db.close()
    threading.Thread(target=_run, daemon=True, name="youtube-follows").start()


def tell_clients(db: Session, live: bool = False) -> None:
    """Open home screens re-read their rows (and with `live` open guides
    their channels): someone's YouTube changed. Open dashboards re-read My
    YouTube, Sources → YouTube, their home rows and My Channels
    (services/live_updates.py; a note says only what changed)."""
    try:
        from services import live_updates
        for topic in ("youtube", "home") + (("mychannels",) if live else ()):
            live_updates.publish(topic, everyone=True)
    except Exception as e:
        logger.debug(f"[YouTube] Dashboards not told: {e}")
    try:
        from services.smartlists import _notify_jellyfin_plugin, bump_playlist_version
        bump_playlist_version()
        _notify_jellyfin_plugin(db)
    except Exception as e:
        logger.debug(f"[YouTube] Clients not told: {e}")
    if live:
        try:
            from services.livetv_tuners import tell_clients as tell_guides
            tell_guides(db)
        except Exception as e:
            logger.debug(f"[YouTube] Guides not told: {e}")
