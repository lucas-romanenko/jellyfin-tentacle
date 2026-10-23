"""My YouTube: each person's own YouTube channels (any signed-in user, for
themselves only). Pasting a link follows the channel: a new one is added and
fetched, one someone else has already added is not fetched twice. Only its
followers see a channel's videos and its Live TV channel
(services/youtube/follows.py). A kids profile's channels are added by an
admin (Sources → YouTube); they see them here, and can't change them.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from models.database import TentacleUser, YouTubeChannel, YouTubeFollow, YouTubeVideo, get_db
from routers import youtube as Y
from routers.auth import get_user_from_request
from services.youtube import follows, indexer

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/my/youtube", tags=["youtube"])


def _state(db: Session, user: TentacleUser) -> dict:
    uid = follows.norm(user.jellyfin_user_id)
    rows = {r.channel_fk: r for r in
            db.query(YouTubeFollow).filter(YouTubeFollow.jellyfin_user_id == uid).all()}
    refresh = Y._refresh_state
    out = []
    for ch in follows.followed_channels(db, uid):
        library_count = db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == ch.id, YouTubeVideo.removed_at.is_(None),
            indexer.is_library_status(YouTubeVideo.live_status)).count()
        live_now = ch.live_enabled and db.query(YouTubeVideo).filter(
            YouTubeVideo.channel_fk == ch.id, YouTubeVideo.live_status == "is_live",
            YouTubeVideo.removed_at.is_(None)).count() > 0
        out.append({
            "id": ch.id, "title": ch.title, "avatar_url": ch.avatar_url,
            "url": ch.input_url, "videos": library_count, "keep_count": ch.keep_count,
            "live_enabled": bool(ch.live_enabled), "live_now": bool(live_now),
            # Still being read for the first time: its videos arrive in a minute or two.
            "loading": ch.last_checked is None,
            "added_by": rows[ch.id].added_by if ch.id in rows else None,
            "others": max(0, len(follows.follower_ids(db, ch.id)) - 1),
        })
    kid = bool(user.kids_level)
    return {
        "channels": out,
        "can_add": not kid,
        "kids": kid,
        "available": Y.client.available(),
        "checking": bool(refresh.get("running")),
    }


@router.get("")
def my_youtube(user: TentacleUser = Depends(get_user_from_request), db: Session = Depends(get_db)):
    return _state(db, user)


class MyChannelAdd(BaseModel):
    url: str
    live: bool = False


def _not_a_kid(user: TentacleUser) -> None:
    if user.kids_level:
        raise HTTPException(403, "A grown-up adds your YouTube channels")


@router.post("")
def add_my_channel(body: MyChannelAdd, request: Request, user: TentacleUser = Depends(get_user_from_request),
                   db: Session = Depends(get_db)):
    """Follow a channel by its link (added and fetched if nobody has it yet).
    The rest of a new channel's settings are the defaults: newest 10, no
    Shorts; an admin can change them on Sources → YouTube."""
    _not_a_kid(user)
    if not (body.url or "").strip():
        raise HTTPException(400, "Paste a YouTube channel or playlist link")
    channel, new = Y.create_channel(db, Y.ChannelCreate(url=body.url.strip(), live=body.live), request,
                                    existing_ok=True)
    if not follows.follow(db, channel, user.jellyfin_user_id):
        raise HTTPException(409, f"'{channel.title}' is already one of your channels")
    Y.after_follow(db, channel, [follows.norm(user.jellyfin_user_id)], new, live=body.live)
    logger.info(f"[YouTube] {user.display_name} follows '{channel.title}'"
                + (" (new)" if new else f" (already fetched for {len(follows.follower_ids(db, channel.id)) - 1} other(s))"))
    return {**_state(db, user), "added": {"id": channel.id, "title": channel.title, "new": new}}


@router.delete("/{channel_id}")
def remove_my_channel(channel_id: int, user: TentacleUser = Depends(get_user_from_request),
                      db: Session = Depends(get_db)):
    """Stop following a channel. The last one to leave removes it."""
    _not_a_kid(user)
    channel: Optional[YouTubeChannel] = db.query(YouTubeChannel).filter(YouTubeChannel.id == channel_id).first()
    if not channel:
        raise HTTPException(404, "Channel not found")
    title = channel.title
    result = Y.leave_channel(db, channel, user.jellyfin_user_id)
    logger.info(f"[YouTube] {user.display_name} no longer follows '{title}'"
                + (" (nobody does now: removed)" if result.get("removed") else ""))
    return {**_state(db, user), "removed": {"id": channel_id, "title": title, "gone": bool(result.get("removed"))}}
