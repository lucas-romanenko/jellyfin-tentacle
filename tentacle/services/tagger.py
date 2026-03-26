"""
Tentacle - Tag Engine
Determines which tags to apply to a piece of content based on:
1. Source category (Netflix, Amazon, etc.)
2. List membership (IMDb Top 250, Trakt, etc.)
3. Recency (Recently Added rolling window)
"""

import logging
from datetime import datetime, timedelta
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
    db: Session
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


def refresh_recently_added_tags(db: Session):
    """
    Periodic job: update Recently Added tags and tag rules for all content.
    - Adds or removes recently-added tags based on date_added vs window.
    - Re-evaluates all tag rules against existing content.
    """
    days = int(get_setting(db, "recently_added_days", "30"))
    cutoff = datetime.utcnow() - timedelta(days=days)

    updated_movies = 0
    updated_series = 0

    # Movies
    movies = db.query(Movie).all()
    for movie in movies:
        tags = list(movie.tags or [])
        is_recent = movie.date_added >= cutoff
        type_label = "Movies"

        recent_tag = f"Recently Added {type_label}"
        source_combo = f"{movie.source_tag} Recently Added {type_label}" if movie.source_tag else None

        # Strip old-format tags and non-source combo tags
        old_format_tags = ["Recently Added"]
        if movie.source_tag:
            old_format_tags.append(f"{movie.source_tag} Recently Added")
        bad_tags = [t for t in tags if t in old_format_tags or (
            "Recently Added" in t and t != recent_tag and t != source_combo
        )]
        if bad_tags:
            tags = [t for t in tags if t not in bad_tags]
            updated_movies += 1

        has_recent = recent_tag in tags
        if is_recent and not has_recent:
            tags.append(recent_tag)
            updated_movies += 1
        elif not is_recent and has_recent:
            tags = [t for t in tags if t != recent_tag]
            updated_movies += 1

        # Source combo tag (e.g. "Netflix Recently Added Movies")
        if source_combo:
            has_source_combo = source_combo in tags
            if is_recent and not has_source_combo:
                tags.append(source_combo)
            elif not is_recent and has_source_combo:
                tags = [t for t in tags if t != source_combo]

        # Migrate old source tags to new format (e.g. "Netflix" → "Netflix Movies")
        if movie.source_tag:
            old_source = movie.source_tag
            new_source = f"{movie.source_tag} {type_label}"
            if old_source in tags and new_source not in tags:
                tags = [new_source if t == old_source else t for t in tags]
                updated_movies += 1
            elif old_source in tags:
                tags = [t for t in tags if t != old_source]
                updated_movies += 1

        movie.tags = tags

    # Series
    series_list = db.query(Series).all()
    for series in series_list:
        tags = list(series.tags or [])
        is_recent = series.date_added >= cutoff
        type_label = "TV"

        recent_tag = f"Recently Added {type_label}"
        source_combo = f"{series.source_tag} Recently Added {type_label}" if series.source_tag else None

        # Strip old-format tags and non-source combo tags
        old_format_tags = ["Recently Added"]
        if series.source_tag:
            old_format_tags.append(f"{series.source_tag} Recently Added")
        bad_tags = [t for t in tags if t in old_format_tags or (
            "Recently Added" in t and t != recent_tag and t != source_combo
        )]
        if bad_tags:
            tags = [t for t in tags if t not in bad_tags]
            updated_series += 1

        has_recent = recent_tag in tags
        if is_recent and not has_recent:
            tags.append(recent_tag)
            updated_series += 1
        elif not is_recent and has_recent:
            tags = [t for t in tags if t != recent_tag]
            updated_series += 1

        # Source combo tag (e.g. "Netflix Recently Added TV")
        if source_combo:
            has_source_combo = source_combo in tags
            if is_recent and not has_source_combo:
                tags.append(source_combo)
            elif not is_recent and has_source_combo:
                tags = [t for t in tags if t != source_combo]

        # Migrate old source tags to new format (e.g. "Netflix" → "Netflix TV")
        if series.source_tag:
            old_source = series.source_tag
            new_source = f"{series.source_tag} {type_label}"
            if old_source in tags and new_source not in tags:
                tags = [new_source if t == old_source else t for t in tags]
                updated_series += 1
            elif old_source in tags:
                tags = [t for t in tags if t != old_source]
                updated_series += 1

        series.tags = tags

    # Re-evaluate tag rules against all content
    rules = db.query(TagRule).filter(TagRule.active == True).all()
    if rules:
        rule_tags_added = 0

        for movie in movies:
            metadata = {
                "genres": movie.genres or [],
                "rating": movie.rating,
                "year": movie.year,
                "runtime": movie.runtime,
                "tags": movie.tags or [],
            }
            rule_tags = apply_tag_rules(metadata, "movie", movie.source or "", movie.source_tag, db)
            tags = list(movie.tags or [])
            changed = False
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)
                    changed = True
            if changed:
                movie.tags = tags
                rule_tags_added += 1

        for series in series_list:
            metadata = {
                "genres": series.genres or [],
                "rating": getattr(series, "rating", None),
                "year": series.year,
                "runtime": None,
                "tags": series.tags or [],
            }
            rule_tags = apply_tag_rules(metadata, "series", series.source or "", series.source_tag, db)
            tags = list(series.tags or [])
            changed = False
            for rt in rule_tags:
                if rt not in tags:
                    tags.append(rt)
                    changed = True
            if changed:
                series.tags = tags
                rule_tags_added += 1

        if rule_tags_added:
            logger.info(f"Tag rules: applied to {rule_tags_added} items")

    db.commit()
    logger.info(f"Tag refresh: updated {updated_movies} movies, {updated_series} series")
    return updated_movies, updated_series
