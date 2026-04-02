"""
Tentacle - Tag Rules Router
Per-user CRUD endpoints for managing automatic tag rules (custom playlists)
"""

import logging
import threading
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

from sqlalchemy import distinct
from models.database import get_db, TagRule, Movie, Series, ListSubscription, TentacleUser
from routers.auth import get_user_from_request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("/condition-options")
def condition_options(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return available values for Source and List condition dropdowns.
    Sources are global (from content), lists are per-user."""
    # Distinct source_tags from synced content (global)
    movie_tags = db.query(distinct(Movie.source_tag)).filter(
        Movie.source_tag.isnot(None), Movie.source_tag != ""
    ).all()
    series_tags = db.query(distinct(Series.source_tag)).filter(
        Series.source_tag.isnot(None), Series.source_tag != ""
    ).all()
    sources = sorted({tag for (tag,) in movie_tags} | {tag for (tag,) in series_tags})

    # Per-user active list subscriptions
    lists = db.query(ListSubscription).filter(
        ListSubscription.active == True,
        ListSubscription.user_id == user.id,
    ).all()
    list_options = [{"name": lst.name, "tag": lst.tag} for lst in lists]
    list_options.sort(key=lambda x: x["name"].lower())

    return {"sources": sources, "lists": list_options}


class ConditionSchema(BaseModel):
    field: str       # genre | rating | year | source | list | runtime
    operator: str    # contains | equals | greater_than | less_than
    value: str


class TagRuleCreate(BaseModel):
    name: str
    output_tag: str
    active: bool = True
    apply_to: str = "both"  # movies | series | both
    conditions: List[ConditionSchema]


class TagRuleUpdate(BaseModel):
    name: Optional[str] = None
    output_tag: Optional[str] = None
    active: Optional[bool] = None
    apply_to: Optional[str] = None
    conditions: Optional[List[ConditionSchema]] = None


@router.get("/rules")
def list_rules(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    rules = db.query(TagRule).filter(
        TagRule.user_id == user.id,
    ).order_by(TagRule.created_at.desc()).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "output_tag": r.output_tag,
            "active": r.active,
            "apply_to": r.apply_to,
            "conditions": r.conditions or [],
            "created_at": r.created_at,
        }
        for r in rules
    ]


@router.post("/rules")
def create_rule(body: TagRuleCreate, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    if not body.conditions:
        raise HTTPException(400, "At least one condition is required")

    rule = TagRule(
        name=body.name,
        output_tag=body.output_tag,
        active=body.active,
        apply_to=body.apply_to,
        conditions=[c.model_dump() for c in body.conditions],
        user_id=user.id,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)

    # Background sync so the new playlist appears in Jellyfin
    _bg_sync_user(user.id)

    return {"id": rule.id, "success": True}


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, body: TagRuleUpdate, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    rule = db.query(TagRule).filter(TagRule.id == rule_id, TagRule.user_id == user.id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")

    if body.name is not None:
        rule.name = body.name
    if body.output_tag is not None:
        rule.output_tag = body.output_tag
    if body.active is not None:
        rule.active = body.active
    if body.apply_to is not None:
        rule.apply_to = body.apply_to
    if body.conditions is not None:
        rule.conditions = [c.model_dump() for c in body.conditions]

    db.commit()

    _bg_sync_user(user.id)

    return {"success": True}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    rule = db.query(TagRule).filter(TagRule.id == rule_id, TagRule.user_id == user.id).first()
    if not rule:
        raise HTTPException(404, "Rule not found")
    db.delete(rule)
    db.commit()

    _bg_sync_user(user.id)

    return {"success": True}


def _bg_sync_user(target_user_id: int):
    """Trigger a background per-user smartlist sync after tag rule changes."""
    def _run():
        from models.database import SessionLocal
        from services.smartlists import sync_smartlists, refresh_smartlist_playlists, write_home_config, _notify_jellyfin_plugin
        bg_db = SessionLocal()
        try:
            sync_smartlists(bg_db, user_id=target_user_id)
            refresh_smartlist_playlists(bg_db, user_id=target_user_id)
            write_home_config(bg_db, user_id=target_user_id)
            _notify_jellyfin_plugin(bg_db)
        except Exception as e:
            logger.error(f"Background smartlist sync failed for user {target_user_id}: {e}")
        finally:
            bg_db.close()

    threading.Thread(target=_run, daemon=True).start()
