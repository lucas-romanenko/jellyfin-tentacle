"""Which XMLTV channel each Live TV channel takes its guide from (#141).

Guide data used to be joined on the provider's tvg-id alone. A channel with no
tvg-id, or one the feed does not carry, got no programmes and no explanation,
and the sync still reported success: on a live install 459 of 840 enabled
channels had no guide.

Ordered passes, most reliable first:
  1. the admin's override (LiveChannel.epg_id_override), which syncs never touch;
  2. the provider's tvg-id, when the feed has that channel;
  3. the channel NAME, reduced by channel_name_key(), and only when it names
     exactly one feed channel of the channel's own country (or one that names
     no country), and no other channel takes that feed channel by name. The
     key strips the "CA:" / "UK:" tag, so a channel whose only namesake in the
     feed was foreign took that foreign guide ("UK: SKY ONE" -> SkyOne.de),
     and DVR rules booked from it. Anything else is reported ("ambiguous",
     "foreign") and never guessed: for a guide, a wrong programme is worse
     than none.
"""
from collections import Counter, defaultdict

from services.channel_names import channel_country, channel_name_key, feed_countries

# How many unmatched / ambiguous channels a coverage report lists by name.
_LIST_LIMIT = 200


def resolve_guide_ids(channels: list, feed_channels: list) -> dict:
    """Match channels to feed channels.

    channels:      [{"id", "name", "tvg_id", "override"}]
    feed_channels: [{"id", "names": [display names]}]

    Returns {channel id: {"method": "override" | "tvg-id" | "name" | None,
                          "guide_id": str | None,    # what programmes are kept under
                          "name_match": str | None,  # set only for method "name"
                          "reason": None | "no-tvg-id" | "tvg-id-not-in-feed" | "ambiguous" | "foreign",
                          "candidates": [feed ids]}} # for "ambiguous" and "foreign"
    """
    feed_ids = {f["id"] for f in feed_channels if f.get("id")}
    countries = {f["id"]: feed_countries(f["id"], f.get("names")) for f in feed_channels if f.get("id")}
    feed_by_key = defaultdict(set)
    for f in feed_channels:
        for name in f.get("names") or []:
            key = channel_name_key(name)
            if key:
                feed_by_key[key].add(f["id"])

    out, needs_name = {}, []
    for ch in channels:
        override = (ch.get("override") or "").strip()
        tvg = (ch.get("tvg_id") or "").strip()
        if override:
            out[ch["id"]] = {"method": "override", "guide_id": override, "name_match": None,
                             "reason": None, "candidates": []}
        elif tvg and (tvg in feed_ids or not feed_ids):
            # A feed that lists no <channel> elements at all cannot be matched
            # by name; its tvg-ids are taken on trust, as they always were.
            out[ch["id"]] = {"method": "tvg-id", "guide_id": tvg, "name_match": None,
                             "reason": None, "candidates": []}
        else:
            needs_name.append(ch)

    tentative = {}
    for ch in needs_name:
        tvg = (ch.get("tvg_id") or "").strip()
        key = channel_name_key(ch.get("name") or "")
        candidates = sorted(feed_by_key.get(key, ())) if key else []
        miss = "tvg-id-not-in-feed" if tvg else "no-tvg-id"
        country = channel_country(ch.get("name") or "")
        if country and candidates:
            local = [c for c in candidates if not countries.get(c) or country in countries[c]]
            if not local:
                out[ch["id"]] = {"method": None, "guide_id": None, "name_match": None,
                                 "reason": "foreign", "candidates": candidates}
                continue
            candidates = local
        if len(candidates) == 1:
            tentative[ch["id"]] = candidates[0]
        elif candidates:
            out[ch["id"]] = {"method": None, "guide_id": None, "name_match": None,
                             "reason": "ambiguous", "candidates": candidates}
        else:
            out[ch["id"]] = {"method": None, "guide_id": None, "name_match": None,
                             "reason": miss, "candidates": []}

    # Two channels that would take one feed channel by name: neither does.
    takers = Counter(tentative.values())
    for cid, guide_id in tentative.items():
        if takers[guide_id] == 1:
            out[cid] = {"method": "name", "guide_id": guide_id, "name_match": guide_id,
                        "reason": None, "candidates": []}
        else:
            out[cid] = {"method": None, "guide_id": None, "name_match": None,
                        "reason": "ambiguous", "candidates": [guide_id]}
    return out


def coverage_report(channels: list, resolved: dict, ids_with_programmes: set) -> dict:
    """What a sync tells the admin: how many channels have a guide, and why not.

    channels: [{"id", "name", "tvg_id", "enabled"}]. Counted over ENABLED
    channels (what Jellyfin shows), with the whole lineup alongside.
    """
    def tally(rows):
        t = {"channels": len(rows), "with_guide": 0, "by_override": 0, "by_tvg_id": 0, "by_name": 0,
             "no_tvg_id": 0, "tvg_id_not_in_feed": 0, "ambiguous": 0, "foreign": 0,
             "matched_but_no_programmes": 0}
        for ch in rows:
            r = resolved.get(ch["id"]) or {}
            if r.get("guide_id"):
                if r["guide_id"] in ids_with_programmes:
                    t["with_guide"] += 1
                    t["by_" + r["method"].replace("-", "_")] += 1
                else:
                    t["matched_but_no_programmes"] += 1
            else:
                t[(r.get("reason") or "no-tvg-id").replace("-", "_")] += 1
        return t

    enabled = [c for c in channels if c.get("enabled")]
    without = []
    for ch in enabled:
        r = resolved.get(ch["id"]) or {}
        if not r.get("guide_id"):
            entry = {"channel_id": ch["id"], "name": ch.get("name"), "tvg_id": ch.get("tvg_id"),
                     "reason": r.get("reason")}
            if r.get("candidates"):
                entry["candidates"] = r["candidates"]
            without.append(entry)

    # One tvg-id on several differently named channels is usually a provider
    # placeholder id ("TS"): they all show one channel's schedule.
    by_tvg = defaultdict(set)
    for ch in enabled:
        r = resolved.get(ch["id"]) or {}
        if r.get("method") == "tvg-id":
            by_tvg[r["guide_id"]].add(channel_name_key(ch.get("name") or "") or ch.get("name"))
    shared = sorted(({"tvg_id": tvg, "channels": len(names), "names": sorted(names)[:10]}
                     for tvg, names in by_tvg.items() if len(names) >= 3),
                    key=lambda s: -s["channels"])

    # Every guide taken by name, for the admin to check: feeds mislabel
    # channels in ways no rule sees, and the per-channel override fixes those.
    by_name = [{"channel_id": ch["id"], "name": ch.get("name"), "guide_id": resolved[ch["id"]]["guide_id"]}
               for ch in enabled if (resolved.get(ch["id"]) or {}).get("method") == "name"]

    return {
        "enabled": tally(enabled),
        "all": tally(channels),
        "by_name": by_name[:_LIST_LIMIT],
        "without_guide": without[:_LIST_LIMIT],
        "shared_tvg_ids": shared[:20],
    }


def coverage_summary(report: dict) -> str:
    """One line for the sync status and Activity."""
    e = report["enabled"]
    parts = [f"guide for {e['with_guide']} of {e['channels']} enabled channels"]
    if e["by_name"]:
        parts.append(f"{e['by_name']} matched by name")
    missing = []
    if e["no_tvg_id"]:
        missing.append(f"{e['no_tvg_id']} have no tvg-id")
    if e["tvg_id_not_in_feed"]:
        missing.append(f"{e['tvg_id_not_in_feed']} a tvg-id the feed lacks")
    if e["ambiguous"]:
        missing.append(f"{e['ambiguous']} an ambiguous name")
    if e["foreign"]:
        missing.append(f"{e['foreign']} a name the feed has only for another country")
    if e["matched_but_no_programmes"]:
        missing.append(f"{e['matched_but_no_programmes']} no programmes in the feed")
    text = ", ".join(parts)
    return text + (f"; {', '.join(missing)}" if missing else "")
