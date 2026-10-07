"""
Tentacle - Tag Engine
Determines which tags to apply to a piece of content based on:
1. Source category (Netflix, Amazon, etc.)
2. List membership (IMDb Top 250, Trakt, etc.)
3. Recency (Recently Added rolling window)
"""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from sqlalchemy import or_
from sqlalchemy.orm import Session

from models.database import (Movie, Series, ListSubscription, ListItem, TagRule, get_setting,
                             get_recently_added_days)

logger = logging.getLogger(__name__)

# Map TMDB production company names → source tags
# Keys are lowercase for case-insensitive matching
STUDIO_TO_SOURCE_TAG = {
    "netflix": "Netflix",
    "amazon studios": "Amazon Prime",
    "amazon prime video": "Amazon Prime",
    "amazon mgm studios": "Amazon Prime",
    "apple tv+": "Apple TV+",
    "apple studios": "Apple TV+",
    "disney+": "Disney+",
    "walt disney pictures": "Disney+",
    "disney television animation": "Disney+",
    "hbo": "HBO",
    "hbo films": "HBO",
    "hbo max": "HBO",
    "max": "HBO",
    "hulu": "Hulu",
    "paramount+": "Paramount+",
    "peacock": "Peacock",
    "showtime": "Showtime",
    "discovery+": "Discovery+",
    "marvel studios": "Marvel",
    "pixar": "Pixar",
    "dreamworks animation": "DreamWorks",
}


def detect_source_tag_from_studios(studios: list) -> Optional[str]:
    """Check TMDB production companies against known streaming services."""
    if not studios:
        return None
    for studio in studios:
        tag = STUDIO_TO_SOURCE_TAG.get(studio.lower())
        if tag:
            return tag
    return None


def compute_tags(
    source_tag: Optional[str],
    date_added: datetime,
    list_tags: List[str],
    recently_added_days: int = 30,
    media_type: str = "movie",
) -> List[str]:
    """
    Compute all tags for a piece of content.
    Returns ordered list of tags.
    media_type: "movie" or "series" — controls type suffix on tags.
    """
    type_label = "Movies" if media_type == "movie" else "TV"
    tags = []

    # 1. Source tag with type suffix (e.g. "Netflix Movies" or "Netflix TV")
    if source_tag:
        tags.append(f"{source_tag} {type_label}")

    # 2. List membership tags (IMDb Top 250, etc.) — no type suffix
    for tag in list_tags:
        if tag not in tags:
            tags.append(tag)

    # 3. Recency tags with type suffix
    cutoff = datetime.utcnow() - timedelta(days=recently_added_days)
    if date_added >= cutoff:
        tags.append(f"Recently Added {type_label}")
        # Source (streaming service) combo only — not list combos
        if source_tag:
            tags.append(f"{source_tag} Recently Added {type_label}")

    return tags


def get_list_tags_for_tmdb_id(
    tmdb_id: int,
    media_type: str,
    db: Session
) -> List[str]:
    """
    Look up which active list subscriptions contain this TMDB ID
    via the ListItem table. Returns the corresponding tags. A film and a
    show with the same TMDB number are different titles (#365).
    """
    list_items = db.query(ListItem).filter(
        ListItem.tmdb_id == tmdb_id, ListItem.of_type(media_type)).all()
    if not list_items:
        return []

    tag_by_list_id = {
        lst.id: lst.tag for lst in db.query(ListSubscription).filter(
            ListSubscription.id.in_([li.list_id for li in list_items]),
            ListSubscription.active == True
        ).all()
    }

    tags = []
    for li in list_items:
        if li.list_id in tag_by_list_id:
            tag = tag_by_list_id[li.list_id]
            if tag not in tags:
                tags.append(tag)
    return tags


def apply_tag_rules(
    metadata: dict,
    media_type: str,
    source: str,
    source_tag: Optional[str],
    db: Session,
    rules: Optional[list] = None,
) -> List[str]:
    """
    Evaluate all active TagRules against content metadata.
    Returns list of tags from matching rules.

    Supported conditions:
    - genre contains X
    - rating greater/less than X
    - year equals/greater/less than X
    - source_tag equals X
    - source equals "radarr" or "provider"
    - runtime greater/less than X (movies only)
    """
    if rules is None:
        rules = db.query(TagRule).filter(TagRule.active == True).all()
    matched_tags = []

    for rule in rules:
        # Check apply_to filter
        if rule.apply_to == "movies" and media_type != "movie":
            continue
        if rule.apply_to == "series" and media_type != "series":
            continue

        if _evaluate_conditions(rule.conditions, metadata, source, source_tag):
            if rule.output_tag not in matched_tags:
                matched_tags.append(rule.output_tag)

    return matched_tags


def rule_metadata(row, media_type: str) -> dict:
    """The metadata a tag rule is evaluated against, for a library row.

    The same shape the nightly pass builds, so every caller that asks "does a
    rule give this row its tag" answers it the way the nightly pass does.
    """
    return {
        "genres": row.genres or [],
        "rating": getattr(row, "rating", None),
        "year": row.year,
        "runtime": getattr(row, "runtime", None) if media_type == "movie" else None,
        "tags": row.tags or [],
    }


def rules_giving(tag: str, db: Session) -> list:
    """Active tag rules whose output is `tag` (any user)."""
    return db.query(TagRule).filter(TagRule.active == True, TagRule.output_tag == tag).all()  # noqa: E712


_bad_rules_logged: set = set()


def rule_gives(rules: list, row, media_type: str, tag: str = None) -> bool:
    """Whether any of `rules` matches this library row.

    `tag` is left out of the row's tags while evaluating: a rule whose own
    condition is "has tag T" and whose output is T would otherwise keep T on a
    title for ever once it had it. A rule that cannot be evaluated (a
    hand-edited or imported row with a malformed condition) counts as not
    matching and is logged once — it must not abort the caller's refresh.
    """
    meta = rule_metadata(row, media_type)
    if tag:
        meta["tags"] = [t for t in meta["tags"] if t != tag]
    for rule in rules:
        if rule.apply_to == "movies" and media_type != "movie":
            continue
        if rule.apply_to == "series" and media_type != "series":
            continue
        try:
            if _evaluate_conditions(rule.conditions, meta, row.source or "", row.source_tag):
                return True
        except Exception as e:
            if rule.id not in _bad_rules_logged:
                _bad_rules_logged.add(rule.id)
                logger.warning(f"[Tags] Tag rule {rule.id} ('{rule.name}') could not be evaluated "
                               f"and is ignored: {e}")
    return False


def tag_taken_by_another_user(db: Session, tag: str, user_id) -> bool:
    """Whether another user's list or tag rule already applies `tag`.

    Tags are library-wide while lists and rules are per user, so a second
    user's list or rule on the same tag merges both users' playlists and lets
    one user's refresh undo the other's. The same user may point a list and a
    rule at one tag (one playlist fed by both). Case-insensitive, as Jellyfin
    compares tags.
    """
    wanted = (tag or "").strip().casefold()
    if not wanted:
        return False
    for model, column in ((ListSubscription, ListSubscription.tag), (TagRule, TagRule.output_tag)):
        for (t,) in db.query(column).filter(or_(model.user_id != user_id, model.user_id.is_(None))):
            if (t or "").strip().casefold() == wanted:
                return True
    return False


def _evaluate_conditions(
    conditions: list,
    metadata: dict,
    source: str,
    source_tag: Optional[str]
) -> bool:
    """All conditions must match (AND logic)"""
    if not conditions:
        return False

    for cond in conditions:
        field = cond.get("field", "")
        operator = cond.get("operator", "")
        value = cond.get("value", "")

        if not _check_condition(field, operator, value, metadata, source, source_tag):
            return False
    return True


def _check_condition(
    field: str, operator: str, value: str,
    metadata: dict, source: str, source_tag: Optional[str]
) -> bool:
    """Evaluate a single condition against content data"""
    if field == "genre":
        genres = [g.lower() for g in (metadata.get("genres") or [])]
        return value.lower() in genres if operator == "contains" else False

    if field == "rating":
        rating = metadata.get("rating") or 0
        try:
            threshold = float(value)
        except (ValueError, TypeError):
            return False
        if operator == "greater_than":
            return rating > threshold
        if operator == "less_than":
            return rating < threshold
        return False

    if field == "year":
        year_str = metadata.get("year") or ""
        try:
            year_val = int(year_str)
            cmp_val = int(value)
        except (ValueError, TypeError):
            return False
        if operator == "equals":
            return year_val == cmp_val
        if operator == "greater_than":
            return year_val > cmp_val
        if operator == "less_than":
            return year_val < cmp_val
        return False

    if field == "source_tag":
        return (source_tag or "").lower() == value.lower() if operator == "equals" else False

    if field == "source":
        # Source condition — matches VOD provider source_tag (e.g. "Netflix", "Amazon")
        if operator == "equals":
            return (source_tag or "").lower() == value.lower()
        return False

    if field == "downloaded":
        if operator == "equals":
            is_radarr = (source == "radarr")
            return is_radarr if value == "yes" else not is_radarr
        return False

    if field == "list":
        # List condition — checks if content has the list's tag applied
        # The tag is passed in via list_tags in compute_tags, but here we check
        # against the metadata's existing tags (applied during sync)
        if operator == "equals":
            content_tags = [t.lower() for t in (metadata.get("tags") or [])]
            return value.lower() in content_tags
        return False

    if field == "runtime":
        runtime = metadata.get("runtime") or 0
        try:
            threshold = int(value)
        except (ValueError, TypeError):
            return False
        if operator == "greater_than":
            return runtime > threshold
        if operator == "less_than":
            return runtime < threshold
        return False

    return False


def builtin_tags(db: Session) -> set:
    """Tags Tentacle derives from a title itself: its source, the recency
    window, "Downloaded". Never taken off because a list or rule stopped
    producing them, whatever a list or rule happens to be called."""
    from models.database import ProviderCategory

    tags = {"Recently Added Movies", "Recently Added TV", "Recently Added",
            "Downloaded Movies", "Downloaded TV"}
    sources = set()
    for model in (Movie, Series, ProviderCategory):
        for (tag,) in db.query(model.source_tag).distinct():
            if tag:
                sources.add(tag)
    for st in sources:
        tags |= {f"{st} Movies", f"{st} TV", f"{st} Recently Added Movies",
                 f"{st} Recently Added TV", f"{st} Recently Added"}
    return tags


def dynamic_tags(db: Session) -> set:
    """Tags that follow list membership and rule matches: every list's and
    rule's tag, active or not, and the tags of deleted lists and rules.
    A title keeps one only while an active list or rule still gives it."""
    tags = {t for (t,) in db.query(ListSubscription.tag).distinct() if t}
    tags |= {t for (t,) in db.query(TagRule.output_tag).distinct() if t}
    tags |= retired_tags(db)
    # A renamed user's old "<name>'s Downloads" is retired, but a user who
    # has that name now still gets it from the Radarr/Sonarr scans (#454).
    return tags - builtin_tags(db) - current_downloads_tags(db)


def paused_tags(db: Session) -> set:
    """List and rule tags left exactly as they are on every title.

    A switched-off list or rule is paused, not deleted: its titles keep the
    tag until it is switched back on (the tag then follows it again) or
    deleted (the tag is retired and comes off). Removing and re-adding every
    tag on each toggle rewrote the NFO of every title it touched. And an
    active list that has never stored an item may hold anything: its first
    read failed or has not happened, and nothing it has not read can be
    judged gone (#145, #168). Tags another active list or rule gives are
    still added where they give them."""
    tags = {t for (t,) in db.query(ListSubscription.tag).filter(ListSubscription.active == False)}  # noqa: E712
    tags |= {t for (t,) in db.query(TagRule.output_tag).filter(TagRule.active == False)}  # noqa: E712
    stored = {lid for (lid,) in db.query(ListItem.list_id).distinct()}
    tags |= {t for (t, lid) in db.query(ListSubscription.tag, ListSubscription.id)
             .filter(ListSubscription.active == True) if lid not in stored}  # noqa: E712
    return {t for t in tags if t}


def list_tag_holders(db: Session) -> dict:
    """{(media_type, tmdb_id): {tags}} from every ACTIVE list's stored items."""
    out = defaultdict(set)
    rows = (db.query(ListSubscription.tag, ListItem.media_type, ListItem.tmdb_id)
            .join(ListItem, ListItem.list_id == ListSubscription.id)
            .filter(ListSubscription.active == True)  # noqa: E712
            .all())
    for tag, media_type, tmdb_id in rows:
        if tag and tmdb_id:
            out[(media_type or "movie", tmdb_id)].add(tag)
    return out


def _row_metadata(row, media_type: str, tags: list) -> dict:
    return {
        "genres": row.genres or [],
        "rating": getattr(row, "rating", None),
        "year": row.year,
        "runtime": getattr(row, "runtime", None) if media_type == "movie" else None,
        "tags": tags,
    }


def reconcile_dynamic_tags(row, media_type: str, tags: list, dynamic: set, holders: dict,
                           rules: list, db: Session, paused: frozenset = frozenset()) -> list:
    """`tags` with the list and rule tags this title should carry, and no others.

    Tags used to be only ever added (#153): a title that stopped matching an
    edited or deleted rule kept its tag, and its playlist entry, for ever. A
    tag several sources produce (two users' lists, a list and a rule, #162) is
    kept while ANY of them holds the title. Everything else on the title,
    Tentacle's built-in tags and tags nobody here wrote, is left as it is, and
    so are the tags of paused lists and rules (see paused_tags).
    """
    held = holders.get((media_type, row.tmdb_id), set())
    kept = [t for t in tags if t not in dynamic or t in held or t in paused]
    for t in sorted(held):
        if t not in kept:
            kept.append(t)
    # Rules see the list tags ("list equals X" is a condition).
    for t in apply_tag_rules(_row_metadata(row, media_type, kept), media_type, row.source or "",
                             row.source_tag, db, rules=rules):
        if t not in kept:
            kept.append(t)
    return kept


def set_row_tags(row, tags: list, owned: Optional[set] = None) -> bool:
    """Store `tags` on a Movie/Series row and in its NFO. True if they changed.

    For .strm titles the NFO is what Jellyfin reads tags from: a tag taken off
    the row but left in the NFO came back at the next metadata refresh (#180).
    Only the <tag> lines are rewritten; everything else in the NFO stays."""
    if list(row.tags or []) == list(tags):
        return False
    row.tags = list(tags)
    if getattr(row, "nfo_path", None):
        from services.nfo import update_nfo_tags
        update_nfo_tags(Path(row.nfo_path), row.tags, owned)
    return True


def tag_conflict(db: Session, tag: str, user_id: Optional[int], list_id: Optional[int] = None,
                 rule_id: Optional[int] = None) -> Optional[str]:
    """Why `tag` cannot name this user's list or custom playlist, or None.

    Tags live on the shared library items, so two users' lists or rules with
    one tag feed one another's playlists (#153, #162), and a list named after
    one of Tentacle's own tags ("Netflix Movies") would compete with it. The
    same user's list and rule may share a name: that is one playlist, their
    union. Compared case-insensitively. Rows with no user are the pre-multi-user
    owner's, and count as this user's.
    """
    wanted = (tag or "").strip().lower()
    if not wanted:
        return "A name is required"
    if wanted in {t.lower() for t in builtin_tags(db)}:
        return f"'{tag}' is one of Tentacle's own playlists — pick a different name"
    for lst in db.query(ListSubscription).filter(ListSubscription.tag.isnot(None)).all():
        if (lst.tag or "").strip().lower() == wanted and lst.id != list_id and lst.user_id not in (None, user_id):
            return f"Another user already has a list or playlist called '{tag}' — pick a different name"
    for rule in db.query(TagRule).filter(TagRule.output_tag.isnot(None)).all():
        if (rule.output_tag or "").strip().lower() == wanted and rule.id != rule_id \
                and rule.user_id not in (None, user_id):
            return f"Another user already has a list or playlist called '{tag}' — pick a different name"
    return None


def tentacle_owned_tags(db: Session) -> set:
    """Every tag name Tentacle itself can put on a Jellyfin item.

    "Refresh Tags" replaces an item's whole tag list with Tentacle's computed
    set, which is the only way a stale Tentacle tag (an expired "Recently
    Added", a deleted list) ever comes off — but it also wiped tags nobody here
    wrote: a `youtube` keyword from a TMDB import, anything a user added by
    hand in Jellyfin (#107). This set lets a caller replace only its own tags
    and keep the rest.

    Built from what the tagger can produce: source tags in both suffix forms,
    the recency and download built-ins, every list subscription's tag and every
    tag rule's output tag — including inactive ones, so a tag that stops being
    produced is still recognised as ours and removed.
    """
    owned = builtin_tags(db)
    for (tag,) in db.query(ListSubscription.tag).distinct():
        if tag:
            owned.add(tag)
    for (tag,) in db.query(TagRule.output_tag).distinct():
        if tag:
            owned.add(tag)
    # ...and every list / rule tag that has since been deleted or renamed. Once
    # the row is gone nothing above produces the tag any more, so without this
    # it read as somebody else's and stayed on every item for ever.
    owned |= retired_tags(db)
    # Each requester's "<name>'s Downloads": the Radarr/Sonarr scans keep it
    # on the rows they attribute, so one on an item no row carries is stale.
    # A renamed user's old one is among the retired tags above (#454).
    owned |= current_downloads_tags(db)
    return owned


def downloads_tag(name: str) -> str:
    """The tag, and the playlist, of one user's downloads ("My Downloads")."""
    return f"{name}'s Downloads"


def current_downloads_tags(db: Session) -> set:
    """Every user's "<name>'s Downloads" under the name they have now."""
    from models.database import TentacleUser
    return {downloads_tag(name) for (name,) in db.query(TentacleUser.display_name).distinct() if name}


def move_downloads_tag(db: Session, user_id: int, old_name: str, new_name: str) -> dict:
    """Give a renamed user's requested titles their new "<name>'s Downloads"
    in place of the old one, on the rows and in the NFOs (#454).

    The Radarr/Sonarr scans would do the same, but until then the renamed
    playlist (which queries the new tag) had nothing to match, and a user
    given the old name next saw these titles in their own "My Downloads".
    Returns {"Movie": {tmdb_id}, "Series": {tmdb_id}} of the titles changed,
    for the Jellyfin push. Does not commit."""
    from models.database import DownloadRequest
    old, new = downloads_tag(old_name), downloads_tag(new_name)
    owned = tentacle_owned_tags(db) | {old}
    changed = {"Movie": set(), "Series": set()}
    for request_type, media_type, model in (("movie", "Movie", Movie), ("series", "Series", Series)):
        requested = {tid for (tid,) in db.query(DownloadRequest.tmdb_id).filter(
            DownloadRequest.user_id == user_id, DownloadRequest.media_type == request_type)}
        if not requested:
            continue
        for row in db.query(model).filter(model.tmdb_id.in_(requested)).all():
            tags = list(row.tags or [])
            if old not in tags:
                continue
            moved = []
            for t in tags:
                t = new if t == old else t
                if t not in moved:
                    moved.append(t)
            try:
                set_row_tags(row, moved, owned)
            except Exception as e:
                # The row is right; the next scan rewrites the NFO.
                logger.warning(f"Could not rewrite the NFO of '{row.title}': {e}")
            changed[media_type].add(row.tmdb_id)
    return changed


RETIRED_TAGS_SETTING = "tentacle_retired_tags"


def retired_tags(db: Session) -> set:
    """Tags Tentacle used to write (a deleted list, a deleted or renamed rule)."""
    import json
    try:
        return set(json.loads(get_setting(db, RETIRED_TAGS_SETTING, "[]") or "[]"))
    except (ValueError, TypeError):
        return set()


def retire_tag(db: Session, tag: str) -> None:
    """Record that `tag` was Tentacle's, so "Refresh Tags" still takes it off
    items after the list or rule that produced it is gone. Call before the
    commit that removes or renames the row; it does not commit."""
    import json
    from models.database import Setting
    if not tag:
        return
    known = retired_tags(db)
    if tag in known:
        return
    known.add(tag)
    row = db.query(Setting).filter(Setting.key == RETIRED_TAGS_SETTING).first()
    value = json.dumps(sorted(known))
    if row:
        row.value = value
    else:
        db.add(Setting(key=RETIRED_TAGS_SETTING, value=value))


def name_key(name) -> str:
    """How two playlist names are compared: NFC-normalised and case-folded.

    Jellyfin compares tags case-insensitively, and the same visible text can
    arrive composed (NFC) or decomposed (NFD) — "Šeimos" typed on one device,
    pasted from another.
    """
    import unicodedata
    return unicodedata.normalize("NFC", (name or "").strip()).casefold()


def youtube_title_taken(db: Session, name: str) -> bool:
    """Whether a YouTube source already has this name. Its playlist comes
    before list and rule playlists in get_desired_smartlists(), so a list or
    rule of the same name would silently get no playlist."""
    from models.database import YouTubeChannel
    key = name_key(name)
    return any(name_key(t) == key for (t,) in db.query(YouTubeChannel.title).all() if t)


def smartlist_names_in_use(db: Session) -> set:
    """name_key of every name a SmartList playlist can have.

    get_desired_smartlists() builds ONE name space, in order: source tags,
    built-ins, per-user Downloads, YouTube sources, lists, tag rules — and
    silently skips any later entry whose name is already taken. So a new name
    has to be checked against all of them, not only its own kind.
    """
    from models.database import TentacleUser, YouTubeChannel
    names = set(tentacle_owned_tags(db))
    names |= {f"{u.display_name}'s Downloads" for u in db.query(TentacleUser).all() if u.display_name}
    names |= {t for (t,) in db.query(YouTubeChannel.title).all() if t}
    return {name_key(n) for n in names if n}


def merge_owned_tags(existing, desired, owned) -> list:
    """Replace Tentacle's own tags in `existing` with `desired`; keep the rest."""
    kept = [t for t in (existing or []) if t not in owned and t not in (desired or [])]
    return kept + list(desired or [])


def _recency_pass(tags: list, is_recent: bool, source_tag: Optional[str], type_label: str,
                  protected: set) -> list:
    """The recency and source tags of one title, brought up to date."""
    recent_tag = f"Recently Added {type_label}"
    source_combo = f"{source_tag} Recently Added {type_label}" if source_tag else None

    # Old-format tags and stale recency combos. A list or rule tag that merely
    # CONTAINS "Recently Added" ("Recently Added on Netflix") is not one of
    # Tentacle's own, and stripping it emptied that playlist every night (#168).
    old_format_tags = ["Recently Added"]
    if source_tag:
        old_format_tags.append(f"{source_tag} Recently Added")
    tags = [t for t in tags if t in protected or not (
        t in old_format_tags or ("Recently Added" in t and t != recent_tag and t != source_combo))]

    if is_recent and recent_tag not in tags:
        tags.append(recent_tag)
    elif not is_recent and recent_tag in tags:
        tags = [t for t in tags if t != recent_tag]

    if source_combo:
        if is_recent and source_combo not in tags:
            tags.append(source_combo)
        elif not is_recent and source_combo in tags:
            tags = [t for t in tags if t != source_combo]

    # Migrate old source tags to new format (e.g. "Netflix" → "Netflix Movies")
    if source_tag and source_tag in tags:
        new_source = f"{source_tag} {type_label}"
        if new_source not in tags:
            tags = [new_source if t == source_tag else t for t in tags]
        else:
            tags = [t for t in tags if t != source_tag]
    return tags


def refresh_recently_added_tags(db: Session):
    """
    Periodic job: bring every title's Tentacle tags up to date.
    - Adds or removes recently-added tags based on date_added vs window.
    - Gives each title exactly the list and rule tags that currently hold it
      (see reconcile_dynamic_tags), so an edited or deleted rule or list
      stops tagging titles it no longer covers.
    Rows (and NFOs) are only written when their tags change.
    """
    days = get_recently_added_days(db)
    cutoff = datetime.utcnow() - timedelta(days=days)

    dynamic = dynamic_tags(db)
    paused = paused_tags(db)
    holders = list_tag_holders(db)
    rules = db.query(TagRule).filter(TagRule.active == True).all()  # noqa: E712
    owned = tentacle_owned_tags(db)
    changed = {"movie": 0, "series": 0}

    for media_type, model, type_label in (("movie", Movie, "Movies"), ("series", Series, "TV")):
        for row in db.query(model).all():
            before = list(row.tags or [])
            is_recent = bool(row.date_added and row.date_added >= cutoff)
            tags = _recency_pass(list(before), is_recent, row.source_tag, type_label, dynamic)
            tags = reconcile_dynamic_tags(row, media_type, tags, dynamic, holders, rules, db, paused)
            if set_row_tags(row, tags, owned):
                changed[media_type] += 1

    db.commit()
    logger.info(f"Tag refresh: updated {changed['movie']} movies, {changed['series']} series")
    return changed["movie"], changed["series"]
