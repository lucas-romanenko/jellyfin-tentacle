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
from sqlalchemy.orm import Session

from models.database import Movie, Series, ListSubscription, ListItem, TagRule, get_setting

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
    via the ListItem table. Returns the corresponding tags.
    """
    list_items = db.query(ListItem).filter(ListItem.tmdb_id == tmdb_id).all()
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
    return tags - builtin_tags(db)


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
                           rules: list, db: Session) -> list:
    """`tags` with the list and rule tags this title should carry, and no others.

    Tags used to be only ever added (#153): a title that stopped matching an
    edited or deleted rule kept its tag, and its playlist entry, for ever. A
    tag several sources produce (two users' lists, a list and a rule, #162) is
    kept while ANY of them holds the title. Everything else on the title,
    Tentacle's built-in tags and tags nobody here wrote, is left as it is.
    """
    held = holders.get((media_type, row.tmdb_id), set())
    kept = [t for t in tags if t not in dynamic or t in held]
    for t in sorted(held):
        if t not in kept:
            kept.append(t)
    # Rules see the list tags ("list equals X" is a condition).
    for t in apply_tag_rules(_row_metadata(row, media_type, kept), media_type, row.source or "",
                             row.source_tag, db, rules=rules):
        if t not in kept:
            kept.append(t)
    return kept


def set_row_tags(row, tags: list) -> bool:
    """Store `tags` on a Movie/Series row and in its NFO. True if they changed.

    For .strm titles the NFO is what Jellyfin reads tags from: a tag taken off
    the row but left in the NFO came back at the next metadata refresh (#180).
    Only the <tag> lines are rewritten; everything else in the NFO stays."""
    if list(row.tags or []) == list(tags):
        return False
    row.tags = list(tags)
    if getattr(row, "nfo_path", None):
        from services.nfo import update_nfo_tags
        update_nfo_tags(Path(row.nfo_path), row.tags)
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
    from models.database import TentacleUser
    for (name,) in db.query(TentacleUser.display_name).distinct():
        if name:
            owned.add(f"{name}'s Downloads")
    return owned


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
    days = int(get_setting(db, "recently_added_days", "30") or 30)
    cutoff = datetime.utcnow() - timedelta(days=days)

    dynamic = dynamic_tags(db)
    holders = list_tag_holders(db)
    rules = db.query(TagRule).filter(TagRule.active == True).all()  # noqa: E712
    changed = {"movie": 0, "series": 0}

    for media_type, model, type_label in (("movie", Movie, "Movies"), ("series", Series, "TV")):
        for row in db.query(model).all():
            before = list(row.tags or [])
            is_recent = bool(row.date_added and row.date_added >= cutoff)
            tags = _recency_pass(list(before), is_recent, row.source_tag, type_label, dynamic)
            tags = reconcile_dynamic_tags(row, media_type, tags, dynamic, holders, rules, db)
            if set_row_tags(row, tags):
                changed[media_type] += 1

    db.commit()
    logger.info(f"Tag refresh: updated {changed['movie']} movies, {changed['series']} series")
    return changed["movie"], changed["series"]
