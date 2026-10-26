"""The listings a rule looks at: exactly the guide Tentacle serves Jellyfin
(routers/livetv.py hdhr_xmltv), per lineup channel (pool key), so what a
rule finds is what Jellyfin can record.

Two sources: the provider's guide (epg_programs, by each channel's guide id
or its folded network's), and event channels' slots, which are built from
the channel's name when the guide is served ("NHL | 01 - 8pm Canadiens at
Blackhawks"; livetv_catalog.event_programmes) and never stored.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class Airing:
    pool_key: str
    channel: str                  # the name people see
    channel_raw: str              # the provider's name and group (event channels name the league there)
    number: int                   # lineup position, for a stable order
    start: datetime               # UTC, naive
    stop: datetime
    title: str
    sub_title: str = ""
    description: str = ""
    category: str = ""
    from_event: bool = False
    previously_shown: Optional[bool] = None   # the guide's <previously-shown> (True) or <new> (False)
    channel_id: Optional[int] = None     # the LiveChannel it streams now (its provider has the connections)
    provider_id: Optional[int] = None
    next: Optional["Airing"] = field(default=None, repr=False)   # what follows on the same channel

    @property
    def minutes(self) -> float:
        return (self.stop - self.start).total_seconds() / 60.0


def airings(db: Session, now: Optional[datetime] = None, hours: int = 14 * 24) -> list:
    """Every listing on the lineup's channels that hasn't ended, within
    `hours`, with .next linked per channel."""
    from models.database import EPGProgram, LiveChannel
    from routers.livetv import _shown_channels, _unique_numbers
    from services import livetv_lineups as L
    from services.epg_categories import infer_category

    now = now or datetime.utcnow()
    until = now + timedelta(hours=hours)
    entries = L.lineup(db)
    by_number: dict = {}
    for e in entries:
        if e["key"][:1] == "y":
            continue   # YouTube channels: not a provider's, nothing to record from a guide
        by_number.setdefault(str(e.get("xmltv_id")), []).append(e)
    if not by_number:
        return []
    ch_ids = {e.get("channel_id") or (int(e["key"][1:]) if e["key"][:1] == "c" and e["key"][1:].isdigit() else None)
              for es in by_number.values() for e in es}
    rows = {c.id: c for c in db.query(LiveChannel).filter(LiveChannel.id.in_([i for i in ch_ids if i])).all()}

    # The guide ids behind each number, as the XMLTV route maps them.
    channels = _shown_channels(db)
    numbered = _unique_numbers(db, channels)
    try:
        from services.livetv_catalog import variant_guides
        network_guide = variant_guides(db)
    except Exception as e:
        logger.warning(f"[Recordings] network guides left out: {e}")
        network_guide = {}
    numbers_of_gid: dict = {}
    group_of: dict = {}
    for ch, number in numbered:
        gid = network_guide.get(ch.id) or ch.guide_epg_id
        group_of[str(number)] = ch.group_title
        if gid and str(number) in by_number:
            numbers_of_gid.setdefault(gid, []).append(str(number))

    out = []

    def add(number, title, sub_title, description, start, stop, category, from_event, previously_shown=None):
        for e in by_number.get(number, ()):
            cid = e.get("channel_id")
            if cid is None and e["key"][:1] == "c" and e["key"][1:].isdigit():
                cid = int(e["key"][1:])
            row = rows.get(cid)
            out.append(Airing(
                pool_key=e["key"], channel=e.get("name") or "", number=int(e.get("number") or 0),
                channel_raw=" ".join(x for x in ((row.name if row else e.get("provider_name")) or "",
                                                 (row.group_title if row else "") or "") if x),
                start=start, stop=stop, title=title or "", sub_title=sub_title or "",
                description=description or "", category=category or "", from_event=from_event,
                previously_shown=previously_shown, channel_id=cid, provider_id=row.provider_id if row else None))

    if numbers_of_gid:
        q = (db.query(EPGProgram.channel_id, EPGProgram.title, EPGProgram.sub_title, EPGProgram.description,
                      EPGProgram.start, EPGProgram.stop, EPGProgram.category, EPGProgram.previously_shown)
             .filter(EPGProgram.channel_id.in_(list(numbers_of_gid)))
             .filter(EPGProgram.stop > now).filter(EPGProgram.start < until))
        for p in q.yield_per(2000):
            for number in numbers_of_gid.get(p.channel_id, ()):
                category = p.category or infer_category(p.title, group_of.get(number)) or ""
                add(number, p.title, p.sub_title, p.description, p.start, p.stop, category, False,
                    p.previously_shown)
    try:
        from services.livetv_catalog import event_programmes
        for p in event_programmes(db, [(ch, n) for ch, n in numbered if str(n) in by_number]):
            if p["stop"] > now and p["start"] < until:
                add(str(p["channel_id"]), p["title"], p.get("sub_title"), p.get("description"),
                    p["start"], p["stop"], p.get("category"), True)
    except Exception as e:   # a channel name must never stop the rules
        logger.warning(f"[Recordings] event channels' slots left out: {e}")

    link_next(out)
    return out


def link_next(items: list) -> None:
    """Each airing's .next: the following listing on the same channel."""
    by_key: dict = {}
    for a in items:
        by_key.setdefault(a.pool_key, []).append(a)
    for same in by_key.values():
        same.sort(key=lambda a: a.start)
        for a, b in zip(same, same[1:]):
            a.next = b
