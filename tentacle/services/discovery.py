"""
Tentacle - New provider content discovery

Nightly sync step: re-fetch each provider's VOD category list and Live TV
group list, upsert anything new (disabled/non-whitelisted by default), and
record what appeared so users get notified. Without this, new categories and
groups only surface when the user manually hits refresh — which they might
never do.

What it feeds:
- Activity feed events (new_categories / new_live_groups) — visible in the
  sync summary/feed
- New from your provider (services/provider_new.py): every group and
  category a refresh creates is "new" until the admin reviews it, counted on
  the sidebar's Sources. The channels of new Live TV groups are read here
  (switched off), so the review shows their counts and the stream checker
  can try them before the admin looks.
"""

import logging
from datetime import datetime

from models.database import (
    Provider, ProviderCategory, LiveChannelGroup,
    log_activity,
)
from services import xtream_client

logger = logging.getLogger(__name__)

# The nightly job runs discovery between the VOD sync and the guide
# download, and its answers are big: an Xtream panel's category counts are two
# whole-catalogue listings (every film, every series), the Live TV channel
# count is the whole channel list (54,000 channels on one household's panel).
# Asked back to back, right after the sync's burst, they were the last thing
# the panel saw before it refused the guide (#709). Gently (the nightly job):
# the counts are fetched only when there is a new category to count, and the
# requests go DISCOVERY_GAP_SECONDS apart.
DISCOVERY_GAP_SECONDS = 10.0


def discover_new_provider_content(db, gentle: bool = False) -> dict:
    """Refresh provider category/group lists and record newly discovered ones.
    `gentle`: the nightly job's way (see DISCOVERY_GAP_SECONDS).

    Returns {"vod_new": [names], "live_new": [names]}.
    """
    result = {"vod_new": [], "live_new": []}
    gap = DISCOVERY_GAP_SECONDS if gentle else 0.0

    # ── VOD categories ────────────────────────────────────────────────────
    # `active` is the VOD switch. Not `live_tv_enabled == False`: the provider
    # test turns Live TV on for every Xtream account that has live channels,
    # so that filter skipped the usual VOD + Live TV provider and its new VOD
    # categories were never found. A provider made on the Live TV page is
    # active=False and stays out.
    vod_providers = (
        db.query(Provider)
        .filter(Provider.active == True)  # noqa: E712
        .all()
    )
    for p in vod_providers:
        try:
            new_names = _refresh_vod_categories(db, p, gentle)
            if new_names:
                logger.info(f"[Discovery] {len(new_names)} new VOD categories from '{p.name}': {new_names[:5]}")
                result["vod_new"].extend(new_names)
        except Exception as e:
            _rollback(db)   # first: reading p.name on a failed session raises again
            logger.warning(f"[Discovery] VOD category refresh failed for '{p.name}': {e}")

    # ── Live TV groups ───────────────────────────────────────────────────
    # The guide's selector: `live_tv_enabled` alone. A provider set up on the
    # Live TV page is active=False, so a filter on `active` skipped the normal
    # Live TV setup (#623, the same mistake #174 fixed for the guide).
    from routers.livetv import live_tv_providers

    for p in live_tv_providers(db):
        try:
            xtream_client.pause(gap)
            new_names = _refresh_live_groups(db, p, gap)
            if new_names:
                logger.info(f"[Discovery] {len(new_names)} new Live TV groups from '{p.name}': {new_names[:5]}")
                result["live_new"].extend(new_names)
        except Exception as e:
            _rollback(db)   # first: reading p.name on a failed session raises again
            logger.warning(f"[Discovery] Live TV group refresh failed for '{p.name}': {e}")

    # ── Notify ───────────────────────────────────────────────────────────
    if result["vod_new"] or result["live_new"]:
        _record_activity(db, result["vod_new"], result["live_new"])

    # ── New groups' channels, for the review ─────────────────────────────
    try:
        from services import provider_new
        todo = [g for g in provider_new._live_groups(db, {provider_new.NEW}) if not provider_new._fresh(g)]
        if todo:
            read = provider_new.read_channels(db, todo)
            logger.info(f"[Discovery] Channels read for {read} new Live TV group(s)")
    except Exception as e:
        _rollback(db)
        logger.warning(f"[Discovery] New groups' channels not read: {e}")

    return result


def _rollback(db) -> None:
    """After a provider's refresh failed: a failed flush leaves the session
    refusing every statement until it is rolled back, so every later provider,
    and the new-content notice, failed with it. Drops only that provider's
    uncommitted rows (they are found again next time)."""
    try:
        db.rollback()
    except Exception as e:
        logger.warning(f"[Discovery] Could not roll back the session: {e}")


# An Xtream panel's category counts are two whole-catalogue listings (see
# DISCOVERY_GAP_SECONDS): gently they are read on a night a category is new,
# else once every VOD_COUNTS_EVERY_DAYS. The categories the VOD sync reads
# get their counts from it every night.
VOD_COUNTS_EVERY_DAYS = 7
_VOD_COUNTS_KEY = "vod_category_counts_read_{}"


def _vod_counts_due(db, provider) -> bool:
    from models.database import get_setting
    try:
        last = datetime.fromisoformat(get_setting(db, _VOD_COUNTS_KEY.format(provider.id), "") or "")
    except (TypeError, ValueError):
        return True
    return (datetime.utcnow() - last).total_seconds() >= VOD_COUNTS_EVERY_DAYS * 86400


def _refresh_vod_categories(db, provider, gentle: bool = False) -> list:
    """Fetch the provider's VOD/series category list, as Sources' "Fetch
    categories" does (routers/providers.fetch_categories): new categories are
    added (not chosen), existing ones get the provider's name and count, and
    an M3U playlist's groups it no longer lists as films or series go unless
    chosen. Whether a category is chosen, its source tag and the rest of the
    admin's settings stay. Returns names of newly discovered categories."""
    from routers.providers import fetch_provider_categories, fetch_category_counts, detect_source_tag

    m3u = (provider.provider_type or "xtream") in ("m3u_url", "m3u_file")
    # Gently, an Xtream panel's counts are asked for only when due
    lazy = gentle and not m3u
    read = {}
    if m3u:
        vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(provider, read)
    elif lazy:
        vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(
            provider, counts=False, gap=DISCOVERY_GAP_SECONDS)
    else:
        vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(provider)

    existing = {
        (c.category_id, c.type): c
        for c in db.query(ProviderCategory).filter(ProviderCategory.provider_id == provider.id).all()
    }
    first_setup = not existing
    counts_read = not lazy
    if lazy and (_vod_counts_due(db, provider) or any(
            (str(c["category_id"]), t) not in existing
            for t, cats in (("movie", vod_cats), ("series", series_cats)) for c in cats)):
        xtream_client.pause(DISCOVERY_GAP_SECONDS)
        vod_counts, series_counts = fetch_category_counts(provider, gap=DISCOVERY_GAP_SECONDS)
        counts_read = True
        if vod_counts or series_counts:
            from models.database import set_setting
            set_setting(db, _VOD_COUNTS_KEY.format(provider.id), datetime.utcnow().isoformat())

    new_names = []
    created = []
    renamed = 0
    now = datetime.utcnow()
    for cat_type, cats, counts in [("movie", vod_cats, vod_counts), ("series", series_cats, series_counts)]:
        # No title of this type in the answer is a blip, not every category
        # emptying at once: existing counts stay (they are the VOD sync's
        # memory that a category held titles, #25). As fetch_categories.
        blip = not any(counts.values())
        for cat in cats:
            cid = str(cat["category_id"])
            cname = cat["category_name"]
            row = existing.get((cid, cat_type))
            if row is not None:
                if cname and row.category_name != cname:
                    row.category_name = cname
                    renamed += 1
                if counts_read and not (blip and row.title_count):
                    row.title_count = counts.get(cid, 0)
                row.last_seen = now
                continue
            row = ProviderCategory(
                provider_id=provider.id,
                category_id=cid,
                category_name=cname,
                type=cat_type,
                whitelisted=False,
                source_tag=detect_source_tag(cname),
                title_count=counts.get(cid, 0),
                last_seen=now,
            )
            db.add(row)
            created.append(row)
            existing[(cid, cat_type)] = row
            new_names.append(cname)

    removed = _drop_unlisted_m3u_categories(db, provider, existing, vod_cats, series_cats) \
        if m3u and read.get("entries") else 0

    if renamed or removed:
        logger.info(f"[Discovery] '{provider.name}': {renamed} VOD categor{'ies' if renamed != 1 else 'y'} "
                    f"renamed by the provider, {removed} no longer listed removed")
    if new_names:
        from services import provider_new
        provider_new.note_created(db, created, first_setup)
    db.commit()
    return new_names


def _drop_unlisted_m3u_categories(db, provider, existing: dict, vod_cats: list, series_cats: list) -> int:
    """fetch_categories' rule for a playlist: its categories are its group
    titles, and one it no longer lists as films or series goes unless
    chosen. Not when that would be more than a fifth of them (at least the
    livetv M3U floor): a truncated playlist is no catalogue change, and a
    category dropped by mistake would come back as new for the review."""
    from models.database import CategorySnapshot
    from routers.livetv import M3U_MAX_REMOVAL_FRACTION, M3U_MIN_REMOVAL_FLOOR
    listed = {(str(c["category_id"]), "movie") for c in vod_cats} | \
             {(str(c["category_id"]), "series") for c in series_cats}
    going = [row for key, row in existing.items() if key not in listed and not row.whitelisted
             and row.id is not None]
    if not going:
        return 0
    if len(going) > max(M3U_MIN_REMOVAL_FLOOR, int(len(existing) * M3U_MAX_REMOVAL_FRACTION)):
        logger.warning(f"[Discovery] '{provider.name}': the playlist no longer lists {len(going)} of "
                       f"{len(existing)} VOD categories — looks truncated, none removed")
        return 0
    for row in going:
        db.query(CategorySnapshot).filter(CategorySnapshot.category_id == row.id).delete(
            synchronize_session=False)
        db.delete(row)
    return len(going)


def _refresh_live_groups(db, provider, gap: float = 0.0) -> list:
    """Fetch + upsert the provider's Live TV groups. An M3U provider's
    playlist is read again whole, as its own sync does: its new groups, and
    its channels' links, which a provider may change (new tokens or host).
    Returns names of newly discovered groups."""
    before = {
        g.name for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider.id).all()
    }
    if (provider.provider_type or "xtream") in ("m3u_url", "m3u_file"):
        from routers.livetv import _snapshot_provider, _sync_from_m3u
        _sync_from_m3u(_snapshot_provider(provider, provider.provider_type), db, "Reading the playlist...")
        after = {
            g.name for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider.id).all()
        }
        return sorted(after - before)
    if (provider.provider_type or "xtream") != "xtream":
        return []

    from routers.livetv import _sync_groups_from_xtream

    provider_data = {
        "id": provider.id,
        "name": provider.name,
        "server_url": provider.server_url,
        "username": provider.username,
        "password": provider.password,
        "user_agent": provider.user_agent or "TiviMate/4.7.0 (Linux; Android 12)",
    }
    _sync_groups_from_xtream(provider_data, db, gap=gap)
    db.commit()

    after = {
        g.name for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider.id).all()
    }
    return sorted(after - before)


# The nightly job reads the channels of every Xtream Live TV provider's
# enabled groups again, as the Live TV page's channel sync does (one listing
# per group). Without it a channel's name changed only on a manual sync, and
# an event channel's name is the day's game ("NHL 03: Leafs at Bruins Oct 26
# 7:00PM ET"): the names went stale, last week's events stayed in the
# lineup's past and today's never came in (26 Oct 2026). An M3U provider's
# playlist is already read whole by _refresh_live_groups. Gently: listings
# CHANNEL_GAP_SECONDS apart, as the VOD sync's (services/sync.py).
CHANNEL_GAP_SECONDS = 5.0


def refresh_live_channels(db, gentle: bool = False, run: str = "nightly") -> list:
    """The channel sync of every Xtream Live TV provider with an enabled
    group: names, links, logos, guide ids and the provider's new channels
    (in an enabled group, switched on, as a manual sync makes them). A
    channel's enabled flag, number and own name stay. Stands aside like the
    manual sync: for a provider whose sync runs, and while a recording runs
    under recording protection. Activity says how many names changed
    ("Live TV channel refresh 08:00: 312 names changed"), so the hour the
    provider renames its event channels shows. `run` names the run there
    ("nightly", "08:00"). Returns the ids of the providers synced."""
    from models.database import LiveChannelGroup as Group
    from routers.livetv import (_get_sync_status, _set_sync_status, _snapshot_provider,
                                _sync_channels_from_xtream, live_tv_providers)
    from services.log_redaction import redact
    from services.provider_activity import recording_protected

    gap = CHANNEL_GAP_SECONDS if gentle else 0.0
    synced = []
    renamed = 0
    for p in live_tv_providers(db):
        if (p.provider_type or "xtream") != "xtream":
            continue
        pid, name = p.id, p.name
        if not db.query(Group.id).filter(Group.provider_id == pid, Group.enabled == True).first():  # noqa: E712
            continue
        if _get_sync_status(pid).get("phase") in ("starting", "running"):
            logger.info(f"[Discovery] Live TV channels of '{name}' not read: a sync of it is running")
            continue
        if recording_protected(db):
            logger.info("[Discovery] Live TV channels not read: a recording is running and recording "
                        "protection is on")
            break
        _set_sync_status(pid, {"phase": "starting", "progress": 0, "message": "Starting channel sync..."})
        try:
            xtream_client.pause(gap)
            stats = _sync_channels_from_xtream(_snapshot_provider(p, "xtream"), db, gap=gap)
        except Exception as e:
            _rollback(db)
            logger.warning(f"[Discovery] Live TV channel sync failed for '{name}': {e}")
            # It has no _run_channel_sync_background to end its status: left
            # "running", every later sync would answer "already in progress".
            _set_sync_status(pid, {"phase": "error", "progress": 0, "message": redact(str(e))})
            what = "Nightly Live TV channel sync" if run == "nightly" else f"Live TV channel refresh {run}"
            log_activity(db, "livetv_sync", f"{what} for {name} failed: {redact(str(e))}")
            continue
        _set_sync_status(pid, {"phase": "complete", "progress": 100, **stats,
                               "message": f"Done: {stats.get('new', 0)} new, {stats.get('updated', 0)} updated, "
                                          f"{stats.get('total', 0)} total"})
        synced.append(pid)
        renamed += stats.get("renamed", 0)
    if synced:
        msg = f"Live TV channel refresh {run}: {renamed} name{'' if renamed == 1 else 's'} changed"
        logger.info(f"[Discovery] {msg}")
        log_activity(db, "livetv_sync", msg)
    return synced


def _record_activity(db, vod_new: list, live_new: list):
    """Activity events for what appeared (the review itself is
    services/provider_new.py)."""
    if vod_new:
        names = ", ".join(vod_new[:3]) + (f" and {len(vod_new) - 3} more" if len(vod_new) > 3 else "")
        log_activity(db, "new_categories",
                     f"{len(vod_new)} new VOD categor{'ies' if len(vod_new) != 1 else 'y'} from provider: {names}")
    if live_new:
        names = ", ".join(live_new[:3]) + (f" and {len(live_new) - 3} more" if len(live_new) > 3 else "")
        log_activity(db, "new_live_groups",
                     f"{len(live_new)} new Live TV group{'s' if len(live_new) != 1 else ''} from provider: {names}")
