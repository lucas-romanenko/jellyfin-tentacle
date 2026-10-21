"""Event / PPV channels: what is on, read from the channel's name.

Providers run numbered event slots ("US: ESPN+ PPV 17", ":TSN+ 40", "NHL | 02 -")
whose guide is empty; the event is only in the name, which the provider
rewrites as events come and go:

    "NHL | 02 - 7:30pm Panthers at Senators"
    "NBA: Chicago vs Toronto @ Oct 21 7:00 PM :Sportsnet+  08"
    "NEXT | ORL - MAGIC VS ATL - HAWKS | Wed 21 Oct 18:00 EDT (US) | 8K EXCLUSIVE | US: NBA PASS PPV 1"
    "US (ESPN+ 011) | American University vs. Army Oct 21 1:00PM ET (2026-10-21 13:00:00)"
    "MLB 01 : Rays x Guardians start:2026-10-18 22:08:00 stop:2026-10-19 05:21:20"
    "PPV EVENT 01: No Mercy 17 (10.21 4:45 PM ET)"
    "- NO EVENT STREAMING - | 8K EXCLUSIVE | US: NBA PASS PPV 12"

`parse()` turns a name into None (not an event channel), "no_event" (an
event slot with nothing on), or {"title", "start", "stop", "timed",
"description"}: start/stop timezone-aware (UTC). A time without a date is
today in the server's time zone (the next occurrence: a time more than the
event's length ago is tomorrow); a date without a year is the nearest such
date; no time at all is a block from the current hour. Events are ~3 h
(sports) unless the name gives the end. The XMLTV guide (routers/livetv.py)
serves one programme per event channel that has no guide of its own.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo

from services.livetv_names import _UPPER_WORDS, _plain

EVENT_HOURS = 3
UNTIMED_HOURS = 6
# An event whose end is longer ago than this is gone: the slot shows nothing.
STALE_HOURS = 12

_TZ = {
    "ET": "America/New_York", "EST": "-05:00", "EDT": "-04:00",
    "CT": "America/Chicago", "CST": "-06:00", "CDT": "-05:00",
    "MT": "America/Denver", "MST": "-07:00", "MDT": "-06:00",
    "PT": "America/Los_Angeles", "PST": "-08:00", "PDT": "-07:00",
    "AT": "America/Halifax", "AST": "-04:00", "ADT": "-03:00",
    "NT": "America/St_Johns", "NST": "-03:30", "NDT": "-02:30",
    "GMT": "+00:00", "UTC": "+00:00", "Z": "+00:00", "BST": "+01:00",
    "CET": "+01:00", "CEST": "+02:00", "UK": "Europe/London",
}
_TZ_RE = r"(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|AT|AST|ADT|NT|NST|NDT|GMT|UTC|BST|CET|CEST)"

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
_MON = r"(?:JAN|FEB|MAR|APR|MAY|JUNE?|JULY?|AUG|SEPT?|OCT|NOV|DEC)[A-Z]*\.?"
_DOW = r"(?:MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*,?"
_ORD = r"(?:ST|ND|RD|TH)?"
_TIME = r"(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?(?::\d{2})?\s*(?P<ap>[AP]\.?M\.?)?"

# Things a slot says when it carries nothing.
_NO_EVENT_RE = re.compile(
    r"NO\s+EVENT|NO\s+SCHEDULED|NO\s+STREAM|NOT\s+STREAMING|OFF\s*AIR|OFFLINE|COMING\s+SOON|"
    r"\b2098-12-31\b|^\W*$", re.I)

# Slot labels: what makes a name an event slot, and what is cut from the title.
_SLOT_PATTERNS = [
    r"\bPPV\s*\d+\b", r"\bPPV\s+EVENT\s*\d+", r"\bLIVE\s+EVENTS?\s*\d+", r"\bEVENTS?\s*\d+\b",
    r"\b(?:ESPN|TSN|SPORTSNET|VICTORY|PEACOCK|PARAMOUNT|FLO|MILB)\s*\+\s*\d+\b", r"\bDAZN\s*\d+\b",
    r"(?:^|\s):\s*[A-Z][A-Z0-9+ ]{1,20}?\s+\d{1,4}\s*$",       # ":Sportsnet+  08", ":Milb  02"
    r"^\s*\(?\s*(?:US|CA|UK)?\s*\(\s*[A-Z+ ]+\s+\d{1,4}\s*\)",  # "US (ESPN+ 003)", "(Victory+ 001)"
    r"^\s*\(\s*[A-Z][A-Z+ ]*\s+\d{1,4}\s*\)",
    r"^\s*(?:NFL|NHL|NBA|MLB|MLS|NCAAF?|NCAAB|UFC|WWE|BOXING|CFL|CBC|WNBA|EPL|F1)\s*\|?\s*\d{1,3}\s*[:|\-]",
    r"^\s*(?:NFL|NHL|NBA|MLB)\s*\|\s*(?:\d{1,3}|4K)\s*-",
    r"^\s*(?:[A-Z]{2}\s*\|\s*)?CFL\s*\d+\s*:",
    r"^\s*(?:CHL|WHL\s+TV)\s*\d+\b",
    r"\bLEAGUE\s+PASS\s*\d+\b", r"\bSUPER\s+SPORTS\s+PACK\s*\d+\b", r"\bPPV\s+SPORTS\s*\d+\b",
    r"^\s*BOXING\s*\d+\b",
]
_SLOT_RE = re.compile("|".join(f"(?:{p})" for p in _SLOT_PATTERNS), re.I)
# Groups whose channels are event slots when their name has a slot number or an event.
_EVENT_GROUP_RE = re.compile(r"\bPPV\b|\bEVENTS?\b|\bLIVE\s+SPORTS?\b|\bPASS\b|\+", re.I)
# Two sides of a match: "A vs B", "A @ B", "A x B", "A v B", "A at B".
_VERSUS_RE = re.compile(r"\S\s+(?:VS\.?|V\.?|@|X|AT)\s+\S", re.I)
_TIME_ONLY_RE = re.compile(r"(?<![\d:/.\-])(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\b|(?<![\d:/.\-])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])", re.I)

_STATUS_WORDS = {"NEXT", "LIVE", "ENDED", "END", "UPCOMING", "GAME REPLAYS", "REPLAY", "NOW", "SPORTS",
                 "8K EXCLUSIVE", "4K EXCLUSIVE", "EXCLUSIVE", "ON NOW", "LIVE NOW", "FINAL"}


def _zone(abbr: Optional[str], server_tz):
    if not abbr:
        return server_tz
    z = _TZ.get(abbr.upper())
    if not z:
        return server_tz
    if z[0] in "+-":
        sign = 1 if z[0] == "+" else -1
        h, m = z[1:].split(":")
        return timezone(sign * timedelta(hours=int(h), minutes=int(m)))
    return ZoneInfo(z)


def _hour(h: str, mi: Optional[str], ap: Optional[str]) -> Optional[tuple]:
    h, mi = int(h), int(mi or 0)
    if ap:
        ap = ap.upper()[0]
        if not 1 <= h <= 12:
            return None
        h = (h % 12) + (12 if ap == "P" else 0)
    if h > 23 or mi > 59:
        return None
    return h, mi


_WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _nearest_year(month: int, day: int, now_local: datetime, dow: Optional[str] = None) -> Optional[datetime]:
    """A date without a year: the one from the last ten months or the next
    two (listings name coming events, and keep stale ones for months). A
    weekday given ("Sat 8th Nov") picks the year it fits, up to a year back."""
    today = now_local.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    wd = _WEEKDAYS.index(dow.upper()[:3]) if dow and dow.upper()[:3] in _WEEKDAYS else None
    for y in (now_local.year, now_local.year + 1, now_local.year - 1):
        try:
            d = datetime(y, month, day)
        except ValueError:
            continue
        if wd is not None:
            if d.weekday() == wd and timedelta(days=-400) <= d - today <= timedelta(days=65):
                return d
            continue
        if timedelta(days=-300) <= d - today <= timedelta(days=65):
            return d
    return None


# Date + time forms, most specific first. Each: (regex, kind).
_DT_FORMS = [
    # start:2026-10-18 22:08:00 stop:2026-10-19 05:21:20 (UTC)
    (re.compile(r"START:\s*(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ T](?P<h>\d{2}):(?P<mi>\d{2})(?::\d{2})?"
                r"(?:\s*STOP:\s*(?P<y2>\d{4})-(?P<mo2>\d{2})-(?P<d2>\d{2})[ T](?P<h2>\d{2}):(?P<mi2>\d{2})(?::\d{2})?)?", re.I),
     "iso_utc"),
    # DAZN: 2026-10-21 | 16:00 (GMT)
    (re.compile(r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})\s*\|\s*" + _TIME + r"\s*\(?\s*(?P<tz>" + _TZ_RE + r")?\s*\)?", re.I), "iso"),
    # (2026-10-21 13:00:00)
    (re.compile(r"\(?\s*(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[ T]" + _TIME + r"\s*\)?(?:\s*(?P<tz>" + _TZ_RE + r")\b)?", re.I), "iso"),
    # Wed 21 Oct 18:00 EDT / Sat 8th Nov 3:00 PM ET / 21 Oct 2026 19:00
    (re.compile(r"(?:(?P<dow>" + _DOW + r")\s+)?(?P<d>\d{1,2})" + _ORD + r"\s+(?P<mon>" + _MON + r")(?:\s+(?P<y>\d{4}))?,?\s+(?:@\s*)?"
                + _TIME + r"(?:\s*\(?(?P<tz>" + _TZ_RE + r")\b\)?)?", re.I), "dmy"),
    # Oct 21 7:00 PM / Fri May 16th 10:00PM / Oct 21 2:30AM ET / Sat Apr 12th 9:00PM
    (re.compile(r"(?:(?P<dow>" + _DOW + r")\s+)?(?P<mon>" + _MON + r")\s+(?P<d>\d{1,2})" + _ORD + r"(?:,?\s+(?P<y>\d{4}))?,?\s+(?:@\s*)?"
                + _TIME + r"(?:\s*\(?(?P<tz>" + _TZ_RE + r")\b\)?)?", re.I), "mdy"),
    # 10.21 4:45 PM ET / 10/22 8PM ET / 19/10 10:35
    (re.compile(r"(?<![\d.:/])(?P<a>\d{1,2})[./](?P<b>\d{1,2})(?:[./](?P<y>\d{2,4}))?(?![\d./:])\s*(?:@\s*|-\s*)?"
                + _TIME.replace("(?P<h>\\d{1,2})", "(?P<h>\\d{1,2})(?=[:\\sAPap])") + r"(?:\s*\(?(?P<tz>" + _TZ_RE + r")\b\)?)?", re.I), "ab"),
]
# A bare time: 7:30pm, 6PM, 19:00, 7:00 PM ET.
_BARE_TIME = re.compile(r"(?<![\d:/.\-])(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s*(?P<ap>[AP]\.?M\.?)(?:\s*(?P<tz>" + _TZ_RE + r")\b)?"
                        r"|(?<![\d:/.\-])(?P<h2>[01]?\d|2[0-3]):(?P<mi2>[0-5]\d)(?![\d:])(?:\s*(?P<tz2>" + _TZ_RE + r")\b)?", re.I)


def _find_when(text: str, now: datetime, server_tz):
    """(start, stop or None, (span start, span end) in text) or None."""
    now_local = now.astimezone(server_tz)
    for rx, kind in _DT_FORMS:
        for m in rx.finditer(text):
            g = m.groupdict()
            try:
                if kind == "iso_utc":
                    start = datetime(int(g["y"]), int(g["mo"]), int(g["d"]), int(g["h"]), int(g["mi"]), tzinfo=timezone.utc)
                    stop = None
                    if g.get("y2"):
                        stop = datetime(int(g["y2"]), int(g["mo2"]), int(g["d2"]), int(g["h2"]), int(g["mi2"]), tzinfo=timezone.utc)
                    return start, stop, m.span()
                hm = _hour(g["h"], g.get("mi"), g.get("ap"))
                if hm is None:
                    continue
                tz = _zone(g.get("tz"), server_tz)
                if kind == "iso":
                    day = datetime(int(g["y"]), int(g["mo"]), int(g["d"]))
                elif kind in ("dmy", "mdy"):
                    mon = _MONTHS.get(g["mon"].upper()[:3])
                    if not mon:
                        continue
                    if g.get("y"):
                        day = datetime(int(g["y"]), mon, int(g["d"]))
                    else:
                        day = _nearest_year(mon, int(g["d"]), now_local, g.get("dow"))
                else:  # "ab": month/day or day/month
                    a, b = int(g["a"]), int(g["b"])
                    if g.get("ap") is None and g.get("mi") is None:
                        continue  # "10/22 8" is no time
                    options = []
                    if 1 <= a <= 12 and 1 <= b <= 31:
                        options.append((a, b))
                    if 1 <= b <= 12 and 1 <= a <= 31:
                        options.append((b, a))
                    days = [d for d in (_nearest_year(mo, dd, now_local) for mo, dd in options) if d]
                    if not days:
                        continue
                    day = min(days, key=lambda d: abs((d - now_local.replace(tzinfo=None)).total_seconds()))
                if day is None:
                    if kind in ("dmy", "mdy"):
                        return "stale"   # a date long gone ("Sun 06 Apr" of last year)
                    continue
                start = datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz)
                return start.astimezone(timezone.utc), None, m.span()
            except (ValueError, TypeError):
                continue
    m = _BARE_TIME.search(text)
    if m:
        g = m.groupdict()
        hm = _hour(g["h"], g["mi"], g["ap"]) if g["h"] else _hour(g["h2"], g["mi2"], None)
        if hm:
            tz = _zone(g.get("tz") or g.get("tz2"), server_tz)
            local_now = now.astimezone(tz)
            start = local_now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            # The next occurrence: one that ended already is tomorrow's.
            if start + timedelta(hours=EVENT_HOURS) < local_now:
                start += timedelta(days=1)
            return start.astimezone(timezone.utc), None, m.span()
    return None


def is_event_group(group: Optional[str]) -> bool:
    return bool(group and _EVENT_GROUP_RE.search(_plain(group)))


def _has_slot_number(text: str) -> bool:
    """A number that numbers the slot: at the end ("SPORTSNET PPV 4",
    "REPLAY 4 HD") or before a separator ("NFL | 15 -", "MLB 03 :")."""
    t = re.sub(r"\b(?:\d+\s*(?:K|P|FPS)|HD|SD|FHD|UHD|RAW|HEVC)\b", " ", text, flags=re.I)
    t = re.sub(r"\([^()]*\)", " ", t)
    return bool(re.search(r"(?<![A-Z0-9#])\d{1,4}\s*(?:$|[:|\-])", t.strip(), re.I))


def is_event_channel(name: str, group: Optional[str] = None) -> bool:
    """An event slot (or a channel named after one event)."""
    text = _plain(name or "")
    if _SLOT_RE.search(text):
        return True
    if is_event_group(group) and (_has_slot_number(text) or _VERSUS_RE.search(text) or _NO_EVENT_RE.search(text)
                                  or re.match(r"^\s*(?:NFL|NHL|NBA|MLB)\s*\|", text, re.I)):
        return True
    # Anywhere: "Leafs vs Bruins 19:00" -- two sides and a time.
    return bool(_VERSUS_RE.search(text) and (_TIME_ONLY_RE.search(text)))


# Words that say which slot or service it is, never what is on.
_GENERIC = {
    "NFL", "NHL", "NBA", "MLB", "MLS", "NCAA", "NCAAB", "NCAAF", "NCAAM", "WNBA", "CFL", "WHL", "CHL",
    "OHL", "MILB", "UFC", "WWE", "AEW", "PGA", "F1", "EPL", "ESPN", "TSN", "SPORTSNET", "DAZN", "VICTORY",
    "FUBO", "PRIME", "APPLE", "TV", "PEACOCK", "PARAMOUNT", "FLO", "ROGERS", "SUPER", "PACK", "BALLY",
    "SLING", "PPV", "EVENT", "EVENTS", "LIVE", "REPLAY", "REPLAYS", "GAME", "GAMES", "SPORTS", "SPORT",
    "CHANNEL", "NETWORK", "PASS", "LEAGUE", "HD", "SD", "FHD", "UHD", "4K", "8K", "RAW", "EXCLUSIVE",
    "NEXT", "ENDED", "END", "UPCOMING", "US", "CA", "EN", "FR", "UK", "CTV", "NEWS", "CBC", "BOXING",
    "STREAM", "VIP", "BK", "PLUS", "INFO", "HIGHLIGHTS", "ALTERNATE", "MAIN", "BACKUP", "FEED",
}


def _generic(text: str) -> bool:
    """Only slot / service / league words (and numbers, single letters)."""
    words = re.findall(r"[A-Z0-9]+", (text or "").upper())
    return all(w in _GENERIC or w.isdigit() or len(w) == 1 or re.fullmatch(r"\d+K", w) for w in words)


def _strip_when(text: str) -> str:
    """Every date and time left in a title ("... Oct 21 7:00PM ET")."""
    for rx, _kind in _DT_FORMS:
        text = rx.sub(" ", text)
    return _BARE_TIME.sub(" ", text)


def _clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    # Slot labels in front: "NHL | 01 -", "PPV EVENT 01:", "LIVE EVENT 01 -", "MLB 01 :", "Boxing 05 :",
    # "CHL 51 HD (D): WHL |", "WHL TV 17:", "CA | CFL 00:", "CBC 4:", "TSN+ 04 :", "US (ESPN+ 003) |".
    lead = re.compile(
        r"^\s*(?:[A-Z]{2}\s*\|\s*)?(?:US\s*)?\(\s*[A-Z+ ]+\s+\d{1,4}\s*\)\s*[|:\-]?"
        r"|^\s*(?:[A-Z]{2}\s*\|\s*)?(?:PPV\s+EVENT|LIVE\s+EVENTS?|EVENT|PPV|CHL|WHL\s+TV|CFL|CBC|MLB|NBA|NHL|NFL|NCAAB|TSN\+?|BOXING|"
        r"SPORTSNET\+?|ESPN\+?|DAZN|MILB)\s*\|?\s*\d{1,4}\s*(?:HD|SD|4K)?\s*(?:\([A-Z]\))?\s*[:|\-]?"
        r"|^\s*(?:NFL|NHL|NBA|MLB)\s*\|\s*(?:4K)\s*-", re.I)
    for _ in range(2):
        text = lead.sub("", text, count=1).strip()
    # Slot labels behind: ":Sportsnet+  08", "| US: NBA PASS PPV 1".
    text = re.sub(r"\s*:\s*[A-Z][A-Za-z0-9+ ]{1,20}?\s+\d{1,4}\s*$", "", text).strip()
    text = re.sub(r"\s*\(\s*(?:US|CA|UK)\s*\)\s*$", "", text, flags=re.I)
    # "Football: Good Morning Football (Good Morning Football)" -> one copy.
    m = re.search(r"\(([^()]+(?:\([^()]*\)[^()]*)?)\)\s*$", text)
    if m and m.group(1).strip() and m.group(1).strip().rstrip(" )") in text[:m.start()]:
        text = text[:m.start()]
    text = re.sub(r"^\s*SPORTS?\s*\|\s*", "", text, flags=re.I)
    # Dates left without a time: "Blair & Barker Oct 21", "WED, 10/21 - ESPN FC".
    text = re.sub(r"[\s:]*\b" + _MON + r"\s+\d{1,2}" + _ORD + r"\s*$", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:" + _DOW + r"\s*)?\d{1,2}/\d{1,2}\s*[-_]\s*", "", text, flags=re.I)
    text = re.sub(r"\s*\*\s*$", "", text)
    text = text.replace("_", "-")
    text = re.sub(r"[(\[]\s*[)\]]", " ", text)          # brackets a date was taken out of
    text = re.sub(r"\s*[(\[]\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -|:@,·•")
    return text


_SMALL = {"VS", "VS.", "V", "AT", "OF", "THE", "AND", "IN", "ON", "FOR", "WITH", "A", "AN", "TO", "DE", "LA"}


def _tidy_case(title: str) -> str:
    """"HOU - ROCKETS VS DAL - MAVERICKS" -> "HOU - Rockets vs DAL - Mavericks":
    a title in capitals reads as one; short codes stay capitals."""
    letters = [c for c in title if c.isalpha()]
    if not letters or sum(c.isupper() for c in letters) < 0.9 * len(letters):
        return title
    out = []
    for i, w in enumerate(title.split(" ")):
        core = re.sub(r"[^A-Z0-9.]", "", w.upper())
        if core in _SMALL and i:
            out.append(w.lower())
        elif (len(re.sub(r"[^A-Z]", "", w)) <= 3 or core in _UPPER_WORDS) and core not in _SMALL:
            out.append(w)
        else:
            out.append(re.sub(r"[A-Za-z][A-Za-z']*", lambda m: m.group(0).capitalize(), w))
    return " ".join(out)


def parse(name: str, group: Optional[str] = None, now: Optional[datetime] = None,
          server_tz=None) -> Union[None, str, dict]:
    """None (no event channel), "no_event", or the event: {"title", "start",
    "stop", "timed", "description"} (UTC datetimes)."""
    if not name or not is_event_channel(name, group):
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    server_tz = server_tz or datetime.now().astimezone().tzinfo
    text = _plain(name).strip()
    if _NO_EVENT_RE.search(text):
        return "no_event"

    found = _find_when(text, now, server_tz)
    if found == "stale":
        return "no_event"
    start = stop = None
    if found:
        start, stop, (a, b) = found
        text = (text[:a] + " | " + text[b:]).strip()
    text = _clean_title(_strip_when(text)) if found else _clean_title(text)

    # Pipe-separated names: drop the status, date, quality and slot parts;
    # the first part left is the title, the rest its description.
    parts = [p.strip() for p in re.split(r"\s*\|\s*", text)]
    keep = []
    for p in parts:
        u = re.sub(r"\s+", " ", p.upper()).strip(" -:")
        if not u or u in _STATUS_WORDS or re.fullmatch(r"\(?(?:US|CA|UK|" + _TZ_RE + r")\)?", u):
            continue
        if re.fullmatch(r"(?:[A-Z]{2,3}\s*:\s*)?[A-Z0-9 +]*\b(?:PPV|PASS|EVENT|PACK|SPORTS)\s*\d+", u) \
                or re.fullmatch(r"(?:[A-Z]{2}\s*:\s*)?(?:[A-Z]+\s+)?(?:PPV|EVENT)\s*\d+(?:\s*\(?[A-Z]\)?)?", u):
            continue
        if re.fullmatch(r"(?:[A-Z]{2}\s*:\s*)?[A-Z0-9 +]+\s+\d{1,4}", u) and _SLOT_RE.search(u):
            continue
        keep.append(p)
    if len(keep) > 1 and any(not _generic(p) for p in keep):
        keep = [p for p in keep if not _generic(p)]   # "WHL", "Boxing 1" in front of the event
    title = _tidy_case(_clean_title(keep[0])) if keep else ""
    description = " - ".join(_clean_title(p) for p in keep[1:] if _clean_title(p)) or None
    # A slot label alone ("MLB 03 :", ":Milb 02", "PPV EVENT 02", "TSN+ 04 :") is no event.
    if not title or _SLOT_RE.fullmatch(title) or re.fullmatch(r"[\W\d_]*", title) \
            or re.fullmatch(r"(?:[A-Z]{2}\s*:\s*)?[A-Z0-9 +]*\b(?:PPV|EVENT|PASS)\b[\sA-Z0-9]*", title.upper()):
        return "no_event"

    timed = start is not None
    if not timed and _generic(title):
        return "no_event"   # "NBA: REPLAY 4 HD", "ROGERS SUPER SPORTS PACK 8"
    if not timed:
        local = now.astimezone(server_tz)
        start = local.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        stop = start + timedelta(hours=UNTIMED_HOURS)
    if stop is None or stop <= start or stop - start > timedelta(hours=8):
        stop = start + timedelta(hours=EVENT_HOURS)
    if stop < now - timedelta(hours=STALE_HOURS):
        return "no_event"
    return {"title": title, "start": start, "stop": stop, "timed": timed, "description": description}
