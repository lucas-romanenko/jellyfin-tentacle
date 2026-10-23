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

logger = logging.getLogger(__name__)


def discover_new_provider_content(db) -> dict:
    """Refresh provider category/group lists and record newly discovered ones.

    Returns {"vod_new": [names], "live_new": [names]}.
    """
    result = {"vod_new": [], "live_new": []}

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
            new_names = _refresh_vod_categories(db, p)
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
            new_names = _refresh_live_groups(db, p)
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


def _refresh_vod_categories(db, provider) -> list:
    """Fetch + upsert the provider's VOD/series category list.
    Returns names of newly discovered categories."""
    from routers.providers import fetch_provider_categories, detect_source_tag

    vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(provider)

    existing = {
        (c.category_id, c.type)
        for c in db.query(ProviderCategory).filter(ProviderCategory.provider_id == provider.id).all()
    }
    first_setup = not existing

    new_names = []
    created = []
    for cat_type, cats, counts in [("movie", vod_cats, vod_counts), ("series", series_cats, series_counts)]:
        for cat in cats:
            cid = str(cat["category_id"])
            cname = cat["category_name"]
            if (cid, cat_type) in existing:
                continue
            row = ProviderCategory(
                provider_id=provider.id,
                category_id=cid,
                category_name=cname,
                type=cat_type,
                whitelisted=False,
                source_tag=detect_source_tag(cname),
                title_count=counts.get(cid, 0),
                last_seen=datetime.utcnow(),
            )
            db.add(row)
            created.append(row)
            existing.add((cid, cat_type))
            new_names.append(cname)

    if new_names:
        from services import provider_new
        provider_new.note_created(db, created, first_setup)
        db.commit()
    return new_names


def _refresh_live_groups(db, provider) -> list:
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
    _sync_groups_from_xtream(provider_data, db)
    db.commit()

    after = {
        g.name for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider.id).all()
    }
    return sorted(after - before)


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
