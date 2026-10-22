"""
Tentacle - Notifications Router
Per-user download completion notifications
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime

from models.database import get_db, Notification, TentacleUser
from routers.auth import get_user_from_request
from services import live_updates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def get_notifications(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Return undismissed notifications for the current user."""
    notifs = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.dismissed_at == None)
        .order_by(Notification.created_at.desc())
        .limit(20)
        .all()
    )
    names = {}
    for_ids = {n.for_user_id for n in notifs if n.for_user_id}
    if for_ids:
        names = dict(db.query(TentacleUser.id, TentacleUser.display_name).filter(TentacleUser.id.in_(for_ids)).all())
    return {
        "notifications": [
            {
                "id": n.id,
                "tmdb_id": n.tmdb_id,
                "media_type": n.media_type,
                "title": n.title,
                "message": n.message,
                "poster_path": n.poster_path,
                "jellyfin_item_id": n.jellyfin_item_id,
                "created_at": n.created_at.isoformat() if n.created_at else None,
                # What the message is about, for the clients' heading: "download"
                # (ready to watch), "request" or "problem" (admins only).
                "kind": n.kind or "download",
                # Whose download it is ("for Sam"), on an admin's message about
                # someone else's; absent for the reader's own.
                **({"for": names[n.for_user_id]} if n.for_user_id in names else {}),
            }
            for n in notifs
        ],
        "notifications_enabled": user.notifications_enabled if user.notifications_enabled is not None else True,
        **({"show_others_downloads": user.show_others_downloads is not False} if user.is_admin else {}),
    }


@router.post("/{notification_id}/dismiss")
def dismiss_notification(notification_id: int, db: Session = Depends(get_db),
                         user: TentacleUser = Depends(get_user_from_request)):
    """Dismiss a single notification."""
    notif = db.query(Notification).filter(
        Notification.id == notification_id, Notification.user_id == user.id
    ).first()
    if not notif:
        raise HTTPException(404, "Notification not found")
    notif.dismissed_at = datetime.utcnow()
    db.commit()
    live_updates.changed("notifications", user_ids=[user.id], clients=True)
    return {"success": True}


@router.post("/dismiss-all")
def dismiss_all(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Dismiss all notifications for the current user."""
    db.query(Notification).filter(
        Notification.user_id == user.id, Notification.dismissed_at == None
    ).update({"dismissed_at": datetime.utcnow()})
    db.commit()
    live_updates.changed("notifications", user_ids=[user.id], clients=True)
    return {"success": True}


@router.post("/toggle")
def toggle_notifications(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Toggle notifications on/off for the current user."""
    current = user.notifications_enabled if user.notifications_enabled is not None else True
    user.notifications_enabled = not current
    db.commit()
    return {"notifications_enabled": user.notifications_enabled}


class OthersBody(BaseModel):
    show: bool


@router.put("/others")
def show_others_downloads(body: OthersBody, db: Session = Depends(get_db),
                          user: TentacleUser = Depends(get_user_from_request)):
    """Admins: other people's downloads in my Activity and messages, or only mine."""
    if not user.is_admin:
        raise HTTPException(403, "Only admins see other people's downloads")
    user.show_others_downloads = body.show
    db.commit()
    live_updates.publish("activity", user_ids=[user.id])
    live_updates.publish("settings", user_ids=[user.id])
    live_updates.tell_clients("activity", user_ids=[user.id])
    return {"show_others_downloads": body.show}
