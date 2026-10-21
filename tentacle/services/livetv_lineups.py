"""The household's Live TV lineup: one for everyone.

The admin chooses which channel groups (and channels) the household has: the
pool, `LiveChannel.enabled` plus the YouTube channels opted into Live TV,
with regional and quality duplicates folded into one network
(services/livetv_catalog.py). The pool is one M3U tuner in Jellyfin and one
guide, numbered 1, 2, 3... in the admin's order. What is personal is the
view, not the tuner: everyone stars their favourites (Jellyfin's favourites,
per person), and My channels is picking those.

Kids profiles see only the household's kids channels: every channel carries
a tag (`tentacle-livetv-kids` on kids channels, `tentacle-livetv-all` on the
rest) and a kid's policy blocks `tentacle-livetv-all` (services/
livetv_tuners.py). A channel new to Jellyfin gets `-all` from the plugin at
once, so a kid never sees it until an admin makes it a kids channel.

Per-person tuners (Tentacle v1.11-1.12 and the "own tuner" work of Oct
2026) are gone: per-person M3U tuners made Jellyfin's guide refresh
quadratic (docs/agents/jellyfin-notes.md). Their addresses are still known
here so a server moving off them finds their channels (favourites,
schedules).

Jellyfin keys an M3U channel on MD5(tuner URL) + MD5(stream URL), not on
its number, so the lineup can be reordered and renumbered freely: favourites
and recordings stay on the channel. The helpers at the bottom compute those
ids the way Jellyfin 10.11 does, so Tentacle knows every channel's id
without asking Jellyfin.
"""
import hashlib
import re
import uuid
from typing import Optional

from sqlalchemy.orm import Session

from models.database import LiveChannel, LiveUserChannel, LiveUserLineup

# Tentacle's tags on Live TV channels (and, by the plugin, their programmes).
TAG_PREFIX = "tentacle-livetv-"
# Every channel of the lineup that isn't a kids channel; kids block it.
ALL_TAG = TAG_PREFIX + "all"
# A kids channel: everyone sees it.
KIDS_TAG = TAG_PREFIX + "kids"

ALL_TUNER_PATH = "/api/live/tuner/all.m3u"
USER_TUNER_RE = re.compile(r"/api/live/users/([0-9a-fA-F]{32})/tuner\.m3u$")


def norm_user_id(user_id: str) -> str:
    return (user_id or "").replace("-", "").lower()


def user_tag(user_id: str) -> str:
    return TAG_PREFIX + norm_user_id(user_id)


def user_tuner_path(user_id: str) -> str:
    return f"/api/live/users/{norm_user_id(user_id)}/tuner.m3u"


def tag_for_tuner_url(url: str) -> Optional[str]:
    """The tag a Tentacle tuner's channels carry, from its URL (a person's
    tuner of before: their own tag); None for any other tuner."""
    path = (url or "").split("?", 1)[0].rstrip("/")
    if path.endswith(ALL_TUNER_PATH):
        return ALL_TAG
    m = USER_TUNER_RE.search(path)
    return user_tag(m.group(1)) if m else None


# ── The pool and each person's list ─────────────────────────────────────────

def pool(db: Session) -> list:
    """Every channel the household has, in the admin's order (else by the
    channel number Jellyfin knew them by): dicts with
    key ("c<id>" / "y<id>"), name, logo_url, xmltv_id (the guide's channel
    id, which is also the old HDHomeRun GuideNumber) and stream_path.

    Channels that are one network in several places or qualities come as
    one entry, key "n<id>" (services/livetv_catalog.py). Every entry also has
    types, has_guide, event (None / "no_event" / {title, start, stop, ...}),
    variants, variant_name and check (the stream checker's verdict)."""
    from routers.livetv import _unique_numbers
    from services.youtube import livetv as youtube_livetv

    channels = (
        db.query(LiveChannel)
        .filter(LiveChannel.enabled == True)  # noqa: E712
        .order_by(LiveChannel.sort_order, LiveChannel.channel_number, LiveChannel.name)
        .all()
    )
    def order(pair):
        # The order Jellyfin showed with the old tuner (by channel number),
        # unless the admin set an order or a number of their own.
        ch, number = pair
        n = str(number)
        return (ch.sort_order or 0, ch.channel_number if ch.channel_number is not None else float("inf"),
                int(n) if n.isdigit() else float("inf"), n, ch.guide_name.lower())

    out = []
    for ch, number in sorted(_unique_numbers(db, channels), key=order):
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
    try:
        from services import livetv_catalog
        return livetv_catalog.fold_pool(db, out, {ch.id: ch for ch in channels})
    except Exception as e:   # the catalog must never take the channels away
        import logging
        logging.getLogger(__name__).exception(f"[LiveTV] Channel catalog failed, pool served unfolded: {e}")
        db.rollback()
        return out


def lineup(db: Session, channels: Optional[list] = None) -> list:
    """What the household's tuner carries, in the pool's order, numbered
    1..N: every pool entry except event channels with no event in their name
    now (an empty slot; it comes back when the provider names an event)."""
    channels = pool(db) if channels is None else channels
    shown = [c for c in channels if c.get("event") != "no_event"]
    return [{**c, "number": i} for i, c in enumerate(shown, 1)]


def kids_tag_of(entry: dict, kids: set) -> str:
    """The tag a lineup channel carries: kids channels the kids tag."""
    return KIDS_TAG if entry["key"] in kids else ALL_TAG


# ── Lists of before (per-person tuners) ─────────────────────────────────────

def lineup_row(db: Session, user_id: str) -> Optional[LiveUserLineup]:
    return db.query(LiveUserLineup).filter(
        LiveUserLineup.jellyfin_user_id == norm_user_id(user_id)).first()


# ── Keys that became other keys ─────────────────────────────────────────────

def aliases(db: Session) -> dict:
    """Old pool key -> the key it became (a channel folded into a network,
    a network merged or split: services/livetv_catalog.py). {} while there
    is no catalog, or it can't say."""
    try:
        from services import livetv_catalog
    except ImportError:
        return {}
    fn = getattr(livetv_catalog, "aliases", None)
    if fn is None:
        return {}
    try:
        return {str(k): str(v) for k, v in (fn(db) or {}).items() if k and v and k != v}
    except Exception as e:   # a list must never break on the catalog
        import logging
        logging.getLogger(__name__).warning(f"[LiveTV] Channel key aliases not read: {e}")
        return {}


def resolve_keys(db: Session, keys: list, known: Optional[set] = None,
                 alias: Optional[dict] = None) -> list:
    """Stored keys as the pool has them now: each followed through the
    aliases (unless it is still a pool key), repeats dropped, order kept.
    With `known`, keys the pool doesn't have are dropped too."""
    alias = aliases(db) if alias is None else alias
    seen, out = set(), []
    for k in keys or []:
        k = str(k)
        hops = 0
        while k in alias and not (known is not None and k in known) and hops < 20:
            k = alias[k]
            hops += 1
        if k in seen or (known is not None and k not in known):
            continue
        seen.add(k)
        out.append(k)
    return out


def stream_path_of_key(key: str) -> Optional[str]:
    """The stream address a pool key has (or had) on a tuner: what Jellyfin
    keyed that channel on, also for a key that is no longer in the pool."""
    key = str(key or "")
    if key[:1] == "c" and key[1:].isdigit():
        return f"/api/live/stream/{key[1:]}"
    if key[:1] == "n" and key[1:].isdigit():
        return f"/api/live/net/{key[1:]}"
    if key[:1] == "y" and len(key) > 1:
        return f"/api/youtube/live/{key[1:]}/stream.ts"
    return None


def stored_keys(db: Session, user_id: str) -> list:
    """Someone's list of before, exactly as saved (old keys not followed):
    what their own tuner carried, while a server moves off it."""
    rows = (db.query(LiveUserChannel)
            .filter(LiveUserChannel.jellyfin_user_id == norm_user_id(user_id))
            .order_by(LiveUserChannel.position, LiveUserChannel.id).all())
    return [r.channel_key for r in rows]


def final_aliases(db: Session, channels: list) -> dict:
    """Every old key -> the pool key it ends up as now (chains followed);
    aliases that end outside the pool are left out."""
    alias = aliases(db)
    known = {c["key"] for c in channels}
    out = {}
    for old in alias:
        if old in known:
            continue
        got = resolve_keys(db, [old], known, alias)
        if got:
            out[old] = got[0]
    return out


def legacy_lineup(db: Session, user_id: str, channels: Optional[list] = None) -> list:
    """What someone's tuner of before carries until the sync removes it:
    their saved list as the pool has it now, numbered."""
    channels = pool(db) if channels is None else channels
    by_key = {c["key"]: c for c in channels}
    keys = resolve_keys(db, stored_keys(db, user_id), set(by_key))
    return [{**by_key[k], "number": i} for i, k in enumerate(keys, 1)]


def forget_user(db: Session, user_id: str) -> None:
    """A person Jellyfin no longer has: their list of before goes too."""
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
    guide on it), tvg-chno the lineup's number. No group-title: people see
    channels, never the provider's groups."""
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


def alias_item_ids_for(tuner_url: str, alias: dict, keep: Optional[set] = None) -> dict:
    """Jellyfin item id -> the current key, for the channels a tuner carried
    under keys that became other keys (alias: old key -> new key, already
    followed to the end). `keep`: only aliases to these keys."""
    base = tuner_base(tuner_url)
    out = {}
    for old, new in alias.items():
        if keep is not None and new not in keep:
            continue
        path = stream_path_of_key(old)
        if path:
            out[m3u_channel_item_id(tuner_url, base + path)] = new
    return out
