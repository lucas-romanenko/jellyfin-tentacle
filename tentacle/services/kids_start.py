"""The ready-made kids setup a new kid starts with (Roadmap 1e; People →
New kids start with, on unless an admin picks something else).

- **Kids home:** Continue Watching, Kids Movies and Kids Shows, with Kids
  Movies in the spotlight. The two playlists are the kid's own genre
  playlists (Animation or Family; shows also Kids), which Tentacle fills by
  asking Jellyfin as the kid, so only what their age limit lets them open
  is in them (services/smartlists.py, native rules: no tags written).
  Discover and Activity are off in their navbar, as for every kid
  (services/family.py).
- **Kids channels as favourites:** the household's kids channels (the only
  channels a kid sees) starred, so the guide's Favorites view is theirs from
  the first day.

It's an ordinary home of their own afterwards: the kid (or an admin signed
in as them) can change it on My Home Screen.
"""
import logging

import requests
from sqlalchemy.orm import Session

from models.database import TagRule, TentacleUser

logger = logging.getLogger(__name__)

# (name, apply_to, genres): TMDB's genre names, as Jellyfin keeps them.
KIDS_PLAYLISTS = [
    ("Kids Movies", "movies", ["Animation", "Family"]),
    ("Kids Shows", "series", ["Animation", "Family", "Kids"]),
]
KIDS_TOOLBAR_OFF = ("discover", "activity")


def _conditions(genres: list) -> list:
    return [{"field": "genre", "operator": "contains", "value": g, "genre_logic": "or"} for g in genres]


def kids_rules(db: Session, user: TentacleUser) -> list:
    """The kid's two genre playlists (made once; a kid who already has one
    keeps theirs as it is)."""
    out = []
    for name, apply_to, genres in KIDS_PLAYLISTS:
        rule = (db.query(TagRule).filter(TagRule.user_id == user.id, TagRule.output_tag == name).first())
        if rule is None:
            rule = TagRule(user_id=user.id, name=name, output_tag=name, apply_to=apply_to,
                           conditions=_conditions(genres), active=True)
            db.add(rule)
            db.commit()
        out.append(rule)
    return out


def kids_home(db: Session, user: TentacleUser) -> int:
    """Start a kid on the kids home; returns the number of rows written."""
    from routers.smartlists import DEFAULT_TOOLBAR
    from services.smartlists import (
        _get_smartlists_with_playlist_ids, _notify_jellyfin_plugin, _user_home_config_path, bump_playlist_version,
        sync_single_custom_playlist, write_home_config, write_home_json,
    )
    from services.default_home import DEFAULT_HERO_OFF, _builtin_row
    for rule in kids_rules(db, user):
        res = sync_single_custom_playlist(db, user.id, rule.output_tag, rule.conditions, rule.apply_to, rule.output_tag)
        if res.get("error"):
            logger.warning(f"[Kids] {user.display_name}'s {rule.output_tag} not made: {res['error']}")
    by_name = {}
    for s in _get_smartlists_with_playlist_ids(db, user_id=user.id):
        by_name.setdefault(s["name"], []).append(s["playlist_id"])
    rows = [_builtin_row("resumevideo")]
    for name, _, _ in KIDS_PLAYLISTS:
        if len(by_name.get(name, [])) == 1:
            rows.append({"type": "playlist", "playlist_id": by_name[name][0], "display_name": name, "shape": "poster"})
    for i, r in enumerate(rows, 1):
        r["order"] = i
    hero = dict(DEFAULT_HERO_OFF)
    first = next((r for r in rows if r["type"] == "playlist"), None)
    if first:
        hero.update(enabled=True, playlist_id=first["playlist_id"], display_name=first["display_name"],
                    require_logo=False)
    toolbar = [{**b, "enabled": False if b["id"] in KIDS_TOOLBAR_OFF else b["enabled"]} for b in DEFAULT_TOOLBAR]
    write_home_json(_user_home_config_path(db, user.id),
                    {"hero": hero, "rows": rows, "toolbar": toolbar, "merge_continue_watching": True})
    write_home_config(db, user_id=user.id)
    bump_playlist_version()
    _notify_jellyfin_plugin(db)
    return len(rows)


def star_kids_channels(db: Session, user: TentacleUser) -> int:
    """Star the household's kids channels for a kid; returns how many were
    starred (0 when Live TV isn't set up in Jellyfin yet)."""
    from routers.my_livetv import my_lineup
    from services import livetv_lineups as L, livetv_tuners as T
    jf, url = T.jellyfin_for(db), T.all_tuner_url(db)
    if jf is None or not url or user.live_tv_off:
        return 0
    uid = L.norm_user_id(user.jellyfin_user_id)
    ids = list(L.item_ids_for(url, my_lineup(db, user)))
    try:
        have = T.favourite_ids(jf, uid)
        new = [i for i in ids if i not in have]
        for item_id in new:
            T.set_favourite(jf, uid, item_id, True)
        return len(new)
    except requests.RequestException as e:
        logger.warning(f"[Kids] {user.display_name}'s kids channels not starred: {e}")
        return 0
