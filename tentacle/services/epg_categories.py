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


def infer_category(title: str = None, group_title: str = None,
                   live_prefix_is_sport: bool = True) -> str | None:
    """Best-guess XMLTV category, or None when nothing is confident enough.

    `live_prefix_is_sport` covers the "Live:" title convention, which on a TV
    listing almost always means a live sporting event. It is off for YouTube,
    where a huge number of ordinary streams are titled that way and the guess
    would mislabel most of them.
    """
    text = (title or "").strip()
    if text and _SPORTS_TITLE.search(text):
        return SPORTS
    if text and live_prefix_is_sport and _LIVE_PREFIX.match(text):
        return SPORTS

    group = (group_title or "").upper()
    if group:
        for needle, category in _GROUP_HINTS:
            if needle in group:
                return category
    return None
