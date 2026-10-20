"""Each person's Live TV channels.

The admin chooses which channel groups (and channels) the household has: the
pool, `LiveChannel.enabled` plus the YouTube channels opted into Live TV.
Everyone sees either the whole pool (the shared "All channels" tuner) or a
list of their own, in their own order, numbered 1, 2, 3... Each list is its
own M3U tuner in Jellyfin, and Jellyfin shows each person only their own
tuner's channels (channels tagged per tuner, the other tags blocked per
user: services/livetv_tuners.py).

Jellyfin keys an M3U channel on MD5(tuner URL) + MD5(stream URL), not on
its number, so a list can be reordered and renumbered freely: favourites and
recordings stay on the channel. The helpers at the bottom compute those ids
the way Jellyfin 10.11 does, so Tentacle knows every person's channel ids
without asking Jellyfin.
"""
import hashlib
import re
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from models.database import LiveChannel, LiveUserChannel, LiveUserLineup

# The tag on every channel of a tuner, and the one each other person blocks.
TAG_PREFIX = "tentacle-livetv-"
ALL_TAG = TAG_PREFIX + "all"

ALL_TUNER_PATH = "/api/live/tuner/all.m3u"
USER_TUNER_RE = re.compile(r"/api/live/users/([0-9a-fA-F]{32})/tuner\.m3u$")


def norm_user_id(user_id: str) -> str:
    return (user_id or "").replace("-", "").lower()


def user_tag(user_id: str) -> str:
    return TAG_PREFIX + norm_user_id(user_id)


def user_tuner_path(user_id: str) -> str:
    return f"/api/live/users/{norm_user_id(user_id)}/tuner.m3u"


def tag_for_tuner_url(url: str) -> Optional[str]:
    """The tag a Tentacle tuner's channels carry, from its URL; None for any
    other tuner."""
    path = (url or "").split("?", 1)[0].rstrip("/")
    if path.endswith(ALL_TUNER_PATH):
        return ALL_TAG
    m = USER_TUNER_RE.search(path)
    return user_tag(m.group(1)) if m else None


# ── The pool and each person's list ─────────────────────────────────────────

def pool(db: Session) -> list:
    """Every channel the household has, in the admin's order: dicts with
    key ("c<id>" / "y<id>"), name, logo_url, xmltv_id (the guide's channel
    id, which is also the old HDHomeRun GuideNumber) and stream_path."""
    from routers.livetv import _unique_numbers
    from services.youtube import livetv as youtube_livetv

    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)  # noqa: E712
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )
    out = []
    for ch, number in _unique_numbers(db, channels):
        out.append({
            "key": f"c{ch.id}",
            "name": ch.guide_name,
            "logo_url": ch.logo_url,
            "xmltv_id": str(number),
            "stream_path": f"/api/live/stream/{ch.id}",
        })
    for yt in youtube_livetv.live_channels(db):
        out.append({
            "key": f"y{yt['youtube_channel_id']}",
            "name": yt["name"],
            "logo_url": yt["logo_url"],
            "xmltv_id": str(yt["guide_number"]),
            "stream_path": f"/api/youtube/live/{yt['youtube_channel_id']}/stream.ts",
        })
    return out


def lineup_row(db: Session, user_id: str) -> Optional[LiveUserLineup]:
    return db.query(LiveUserLineup).filter(
        LiveUserLineup.jellyfin_user_id == norm_user_id(user_id)).first()


def has_own_list(db: Session, user_id: str) -> bool:
    row = lineup_row(db, user_id)
    return bool(row and row.own_list)


def own_keys(db: Session, user_id: str) -> list:
    rows = (db.query(LiveUserChannel)
            .filter(LiveUserChannel.jellyfin_user_id == norm_user_id(user_id))
            .order_by(LiveUserChannel.position, LiveUserChannel.id).all())
    return [r.channel_key for r in rows]


def lineup_for(db: Session, user_id: Optional[str], channels: Optional[list] = None) -> list:
    """What someone's tuner carries, in their order: their own list (minus
    channels the household no longer has), else the whole pool. Each entry
    gets its number, 1..N."""
    channels = pool(db) if channels is None else channels
    if user_id and has_own_list(db, user_id):
        return own_lineup(db, user_id, channels)
    return [{**c, "number": i} for i, c in enumerate(channels, 1)]


def own_lineup(db: Session, user_id: str, channels: Optional[list] = None) -> list:
    """Someone's own list (whichever they use now), numbered."""
    channels = pool(db) if channels is None else channels
    by_key = {c["key"]: c for c in channels}
    chosen = [by_key[k] for k in own_keys(db, user_id) if k in by_key]
    return [{**c, "number": i} for i, c in enumerate(chosen, 1)]


def save_own_list(db: Session, user_id: str, own_list: bool, keys: list) -> None:
    """Keep someone's choice: their own list (keys in order; unknown or
    repeated keys dropped) or all channels. Switching back to all channels
    keeps the list, so switching again brings it back."""
    uid = norm_user_id(user_id)
    row = lineup_row(db, uid)
    if row is None:
        row = LiveUserLineup(jellyfin_user_id=uid, own_list=False)
        db.add(row)
    row.own_list = bool(own_list)
    if own_list:
        known = {c["key"] for c in pool(db)}
        seen, ordered = set(), []
        for k in keys or []:
            k = str(k)
            if k in known and k not in seen:
                seen.add(k)
                ordered.append(k)
        db.query(LiveUserChannel).filter(LiveUserChannel.jellyfin_user_id == uid).delete()
        for pos, k in enumerate(ordered):
            db.add(LiveUserChannel(jellyfin_user_id=uid, channel_key=k, position=pos))
    db.commit()


def forget_user(db: Session, user_id: str) -> None:
    """A person Jellyfin no longer has: their list goes too."""
    uid = norm_user_id(user_id)
    db.query(LiveUserChannel).filter(LiveUserChannel.jellyfin_user_id == uid).delete()
    db.query(LiveUserLineup).filter(LiveUserLineup.jellyfin_user_id == uid).delete()
    db.commit()


# ── The M3U Jellyfin reads ──────────────────────────────────────────────────

def _m3u_text(text) -> str:
    """One M3U field: a line break would start another channel (#260), and a
    double quote would end the attribute."""
    return re.sub(r"[\r\n]+", " ", str(text)).replace('"', "'") if text else ""


def m3u(entries: list, base_url: str) -> str:
    """An M3U tuner: tvg-id is the guide's channel id (Jellyfin matches the
    guide on it), tvg-chno the person's own number. No group-title: people
    pick channels, they never see the provider's groups."""
    base = (base_url or "").rstrip("/")
    lines = ["#EXTM3U"]
    for e in entries:
        logo = f' tvg-logo="{_m3u_text(e["logo_url"])}"' if e.get("logo_url") else ""
        lines.append(f'#EXTINF:-1 tvg-id="{_m3u_text(e["xmltv_id"])}" tvg-chno="{e["number"]}"{logo},'
                     f'{_m3u_text(e["name"])}')
        lines.append(base + e["stream_path"])
    return "\n".join(lines) + "\n"


# ── Jellyfin's ids (10.11) ──────────────────────────────────────────────────

def _jf_md5(text: str) -> uuid.UUID:
    """Jellyfin's string.GetMD5(): MD5 of the UTF-16LE text, read as a .NET Guid."""
    return uuid.UUID(bytes_le=hashlib.md5(text.encode("utf-16-le")).digest())


def m3u_external_id(tuner_url: str, stream_url: str) -> str:
    """M3UTunerHost: "m3u_" + MD5(tuner URL) + MD5(stream URL)."""
    return "m3u_" + _jf_md5(tuner_url).hex + _jf_md5(stream_url).hex


def hdhr_external_id(guide_number: str) -> str:
    return f"hdhr_{guide_number}"


def channel_item_id(external_id: str) -> str:
    """The LiveTvChannel item id (LiveTvDtoService.GetInternalChannelId with
    the built-in service "Emby", LibraryManager.GetNewItemId)."""
    key = ("Emby" + external_id + "4").lower()
    return _jf_md5("MediaBrowser.Controller.LiveTv.LiveTvChannel" + key).hex


def m3u_channel_item_id(tuner_url: str, stream_url: str) -> str:
    return channel_item_id(m3u_external_id(tuner_url, stream_url))


def tuner_base(tuner_url: str) -> str:
    """The address a Tentacle tuner's streams are under: its URL up to /api/."""
    i = (tuner_url or "").find("/api/")
    return tuner_url[:i] if i >= 0 else (tuner_url or "").rstrip("/")


def item_ids_for(tuner_url: str, entries: list) -> dict:
    """Jellyfin item id -> pool key, for every channel a tuner carries."""
    base = tuner_base(tuner_url)
    return {m3u_channel_item_id(tuner_url, base + e["stream_path"]): e["key"] for e in entries}
