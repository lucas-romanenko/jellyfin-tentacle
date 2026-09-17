"""Infer an XMLTV programme category when the provider supplies none.

Jellyfin's XmlTvListingsProvider sets IsSports / IsNews / IsKids / IsMovie from
the `<category>` element, and matches its (configurable) lists case-insensitively
against plain words. Xtream providers commonly send no category at all — every
row NULL on some servers — so nothing was ever flagged: no sports badge in the
guide, empty "Sports" filters, and DVR series-timer defaults for sports (longer
padding) never applying.

Inference is deliberately conservative: the title patterns are anchored on
league and sport names, and the channel group is only consulted when the title
says nothing. A wrong guess is worse than none, since it mislabels the guide.
"""
import re

SPORTS = "Sports"
NEWS = "News"
KIDS = "Kids"
MOVIE = "Movie"

# Leagues, competitions and sport names. Word-anchored so "Cricket" matches but
# "Cricketing Show" style substrings inside other words do not.
_SPORTS_TITLE = re.compile(
    r"\b("
    r"NHL|MLB|NFL|CFL|NBA|WNBA|MLS|UFC|PGA|NCAA|LPGA|NASCAR|Formula\s*1|Grand\s*Prix|"
    r"Hockey|Baseball|Football|Basketball|Soccer|Golf|Tennis|Boxing|Wrestling|"
    r"Curling|Rugby|Cricket|Cycling|Athletics|Motorsport|Darts|Snooker|"
    r"SportsCentre|SportsCenter|Premier\s*League|Champions\s*League|World\s*Cup"
    r")\b",
    re.IGNORECASE,
)
_LIVE_PREFIX = re.compile(r"^\s*live\s*[:\-]", re.IGNORECASE)

# Channel group/category names, checked only when the title is uninformative.
_GROUP_HINTS = (
    ("SPORT", SPORTS),
    ("NEWS", NEWS),
    ("KID", KIDS),
    ("CARTOON", KIDS),
    ("FAMILY", KIDS),
    ("CINEMA", MOVIE),
    ("MOVIE", MOVIE),
)


def infer_category(title: str = None, group_title: str = None) -> str | None:
    """Best-guess XMLTV category, or None when nothing is confident enough."""
    text = (title or "").strip()
    if text and (_SPORTS_TITLE.search(text) or _LIVE_PREFIX.match(text)):
        return SPORTS

    group = (group_title or "").upper()
    if group:
        for needle, category in _GROUP_HINTS:
            if needle in group:
                return category
    return None
