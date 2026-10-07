"""Which XMLTV channel each Live TV channel takes its guide from (#141).

Guide data used to be joined on the provider's tvg-id alone. A channel with no
tvg-id, or one the feed does not carry, got no programmes and no explanation,
and the sync still reported success: on a live install 459 of 840 enabled
channels had no guide.

Ordered passes, most reliable first:
  1. the admin's override (LiveChannel.epg_id_override), which syncs never touch;
  2. the provider's tvg-id, when the feed has that channel (ignoring letter
     case, as Jellyfin does: the feed's own spelling is then the guide id);
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

import re

from services.channel_names import channel_country, channel_name_key, channel_name_words, feed_countries

# How many unmatched / ambiguous channels a coverage report lists by name.
_LIST_LIMIT = 200

# For deciding whether one feed id's display names are ONE channel: a
# timeshift ("+1", "plus 1"), "(BK)" and generic words say nothing about which
# channel it is, and spacing differs between entries ("SPORTSN ET WEST").
_TIMESHIFT = re.compile(r"(?:\s+plus\s+\d{1,2})+$")
_IDENTITY_DROP = {"bk", "channel", "tv"}


def _identity(name: str) -> str:
    words = " ".join(channel_name_words(name))
    words = _TIMESHIFT.sub("", words)
    return "".join(w for w in words.split() if w not in _IDENTITY_DROP)


def _one_channel(names) -> bool:
    """True when the names are one channel's: the same after _identity, or
    one ending the other ("HISTORY" / "DISCOVERY HISTORY": a brand prefix).
    "LRT" / "LRT PLUS" differ at the END: two channels. A name that is only
    a number (a channel number) is not counted."""
    ids = sorted({i for i in (_identity(n) for n in names) if i and not i.isdigit()}, key=len)
    if len(ids) <= 1:
        return True
    longest = ids[-1]
    return all(len(i) >= 3 and longest.endswith(i) for i in ids[:-1])


def _has_timeshift(name: str) -> bool:
    return bool(_TIMESHIFT.search(" ".join(channel_name_words(name))))


def resolve_guide_ids(channels: list, feed_channels: list) -> dict:
    """Match channels to feed channels.

    channels:      [{"id", "name", "tvg_id", "override"}]
    feed_channels: [{"id", "names": [display names]}]

    Returns {channel id: {"method": "override" | "tvg-id" | "name" | None,
                          "guide_id": str | None,    # what programmes are kept under
                          "name_match": str | None,  # the feed id to store, for method "name"
                                                     # and for a tvg-id the feed spells in another case
                          "reason": None | "no-tvg-id" | "tvg-id-not-in-feed" | "ambiguous" | "foreign",
                          "candidates": [feed ids]}} # for "ambiguous" and "foreign"
    """
    feed_ids = {f["id"] for f in feed_channels if f.get("id")}
    # Jellyfin matches a tuner channel's guide id to the feed ignoring letter
    # case, so "cnn.us" in the playlist and "CNN.us" in the feed had a guide
    # there. A tvg-id the feed has only in another case takes the feed's own
    # spelling, when exactly one feed id has it (an exact match always wins).
    feed_ids_by_case = defaultdict(set)
    for fid in feed_ids:
        feed_ids_by_case[fid.lower()].add(fid)
    countries = {f["id"]: feed_countries(f["id"], f.get("names")) for f in feed_channels if f.get("id")}
    feed_by_key = defaultdict(set)
    feed_names = defaultdict(list)     # a feed may list one id in several <channel> elements
    for f in feed_channels:
        if not f.get("id"):
            continue        # a <channel id=""> in the feed is nobody's guide
        feed_names[f.get("id")].extend(f.get("names") or [])
        for name in f.get("names") or []:
            key = channel_name_key(name)
            if key:
                feed_by_key[key].add(f["id"])
    # A feed id whose display names are different channels' names (one real
    # feed lists "LRT PLUS" and "LRT" on one id) cannot be trusted to be
    # either: no channel is matched by name through it.
    mixed = {fid for fid, names in feed_names.items() if fid and not _one_channel(names)}

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
        elif tvg and len(feed_ids_by_case.get(tvg.lower(), ())) == 1:
            # Stored like a name match (LiveChannel.epg_name_match), so the
            # guide is kept and served under the id the feed's programmes use.
            fid = next(iter(feed_ids_by_case[tvg.lower()]))
            out[ch["id"]] = {"method": "tvg-id", "guide_id": fid, "name_match": fid,
                             "reason": None, "candidates": []}
        else:
            needs_name.append(ch)

    tentative = {}
    for ch in needs_name:
        tvg = (ch.get("tvg_id") or "").strip()
        key = channel_name_key(ch.get("name") or "")
        candidates = sorted(feed_by_key.get(key, ())) if key else []
        miss = "tvg-id-not-in-feed" if tvg else "no-tvg-id"
        if candidates and set(candidates) & mixed:
            candidates = [c for c in candidates if c not in mixed]
            if not candidates:
                out[ch["id"]] = {"method": None, "guide_id": None, "name_match": None,
                                 "reason": "ambiguous", "candidates": []}
                continue
        country = channel_country(ch.get("name") or "")
        if country and candidates:
            local = [c for c in candidates if not countries.get(c) or country in countries[c]]
            if not local:
                out[ch["id"]] = {"method": None, "guide_id": None, "name_match": None,
                                 "reason": "foreign", "candidates": candidates}
                continue
            candidates = local
        if len(candidates) > 1:
            # One id carrying the timeshift too ("GOLD", "GOLD +1") next to one
            # that is only the channel ("GOLD"): the plain one is it.
            plain = [c for c in candidates if not any(_has_timeshift(n) for n in feed_names.get(c, ()))]
            if len(plain) == 1:
                candidates = plain
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
