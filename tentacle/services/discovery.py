"""
Tentacle - New provider content discovery

Nightly sync step: re-fetch each provider's VOD category list and Live TV
group list, upsert anything new (disabled/non-whitelisted by default), and
record what appeared so users get notified. Without this, new categories and
groups only surface when the user manually hits refresh — which they might
never do.

Notification surfaces fed from here:
- Activity feed events (new_categories / new_live_groups) — visible in the
  sync summary/feed
- A persistent "new content available" notice (setting: new_content_notice)
  shown as a dismissible dashboard banner, accumulating across nights until
  the user dismisses it
"""

import json
import logging
from datetime import datetime, timezone

from models.database import (
    Provider, ProviderCategory, LiveChannelGroup,
    get_setting, set_setting, log_activity,
)

logger = logging.getLogger(__name__)

# Cap the names stored in the notice — the banner shows a few examples, the
# full list lives on the VOD / Live TV pages anyway.
MAX_NOTICE_NAMES = 15


def discover_new_provider_content(db) -> dict:
    """Refresh provider category/group lists and record newly discovered ones.

    Returns {"vod_new": [names], "live_new": [names]}.
    """
    result = {"vod_new": [], "live_new": []}

    # ── VOD categories ────────────────────────────────────────────────────
    vod_providers = (
        db.query(Provider)
        .filter(Provider.active == True, Provider.live_tv_enabled == False)  # noqa: E712
        .all()
    )
    for p in vod_providers:
        try:
            new_names = _refresh_vod_categories(db, p)
            if new_names:
                logger.info(f"[Discovery] {len(new_names)} new VOD categories from '{p.name}': {new_names[:5]}")
                result["vod_new"].extend(new_names)
        except Exception as e:
            logger.warning(f"[Discovery] VOD category refresh failed for '{p.name}': {e}")

    # ── Live TV groups ───────────────────────────────────────────────────
    live_providers = (
        db.query(Provider)
        .filter(Provider.active == True, Provider.live_tv_enabled == True)  # noqa: E712
        .all()
    )
    for p in live_providers:
        try:
            new_names = _refresh_live_groups(db, p)
            if new_names:
                logger.info(f"[Discovery] {len(new_names)} new Live TV groups from '{p.name}': {new_names[:5]}")
                result["live_new"].extend(new_names)
        except Exception as e:
            logger.warning(f"[Discovery] Live TV group refresh failed for '{p.name}': {e}")

    # ── Notify ───────────────────────────────────────────────────────────
    if result["vod_new"] or result["live_new"]:
        _record_notice(db, result["vod_new"], result["live_new"])

    return result


def _refresh_vod_categories(db, provider) -> list:
    """Fetch + upsert the provider's VOD/series category list.
    Returns names of newly discovered categories."""
    from routers.providers import fetch_provider_categories, detect_source_tag

    vod_cats, series_cats, vod_counts, series_counts = fetch_provider_categories(provider)

    existing = {
        (c.category_id, c.type)
        for c in db.query(ProviderCategory).filter(ProviderCategory.provider_id == provider.id).all()
    }

    new_names = []
    for cat_type, cats, counts in [("movie", vod_cats, vod_counts), ("series", series_cats, series_counts)]:
        for cat in cats:
            cid = str(cat["category_id"])
            cname = cat["category_name"]
            if (cid, cat_type) in existing:
                continue
            db.add(ProviderCategory(
                provider_id=provider.id,
                category_id=cid,
                category_name=cname,
                type=cat_type,
                whitelisted=False,
                source_tag=detect_source_tag(cname),
                title_count=counts.get(cid, 0),
                last_seen=datetime.utcnow(),
            ))
            existing.add((cid, cat_type))
            new_names.append(cname)

    if new_names:
        db.commit()
    return new_names


def _refresh_live_groups(db, provider) -> list:
    """Fetch + upsert the provider's Live TV groups (Xtream only — M3U group
    discovery requires a full playlist parse and is skipped nightly).
    Returns names of newly discovered groups."""
    if (provider.provider_type or "xtream") != "xtream":
        return []

    from routers.livetv import _sync_groups_from_xtream

    before = {
        g.name for g in db.query(LiveChannelGroup).filter(LiveChannelGroup.provider_id == provider.id).all()
    }

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


def _record_notice(db, vod_new: list, live_new: list):
    """Append to the persistent new-content notice + emit activity events."""
    try:
        notice = json.loads(get_setting(db, "new_content_notice", "") or "{}")
    except Exception:
        notice = {}

    vod = list(dict.fromkeys((notice.get("vod") or []) + vod_new))[:MAX_NOTICE_NAMES]
    live = list(dict.fromkeys((notice.get("live") or []) + live_new))[:MAX_NOTICE_NAMES]
    vod_total = (notice.get("vod_total") or 0) + len(vod_new)
    live_total = (notice.get("live_total") or 0) + len(live_new)

    set_setting(db, "new_content_notice", json.dumps({
        "vod": vod,
        "live": live,
        "vod_total": vod_total,
        "live_total": live_total,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }))

    if vod_new:
        names = ", ".join(vod_new[:3]) + (f" and {len(vod_new) - 3} more" if len(vod_new) > 3 else "")
        log_activity(db, "new_categories",
                     f"{len(vod_new)} new VOD categor{'ies' if len(vod_new) != 1 else 'y'} from provider: {names}")
    if live_new:
        names = ", ".join(live_new[:3]) + (f" and {len(live_new) - 3} more" if len(live_new) > 3 else "")
        log_activity(db, "new_live_groups",
                     f"{len(live_new)} new Live TV group{'s' if len(live_new) != 1 else ''} from provider: {names}")
