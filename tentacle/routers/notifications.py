"""
Tentacle - Notifications Router
Per-user download completion notifications
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from models.database import get_db, Notification, TentacleUser
from routers.auth import get_user_from_request

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
            }
            for n in notifs
        ],
        "notifications_enabled": user.notifications_enabled if user.notifications_enabled is not None else True,
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
    return {"success": True}


@router.post("/dismiss-all")
def dismiss_all(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Dismiss all notifications for the current user."""
    db.query(Notification).filter(
        Notification.user_id == user.id, Notification.dismissed_at == None
    ).update({"dismissed_at": datetime.utcnow()})
    db.commit()
    return {"success": True}


@router.post("/toggle")
def toggle_notifications(db: Session = Depends(get_db), user: TentacleUser = Depends(get_user_from_request)):
    """Toggle notifications on/off for the current user."""
    current = user.notifications_enabled if user.notifications_enabled is not None else True
    user.notifications_enabled = not current
    db.commit()
    return {"notifications_enabled": user.notifications_enabled}
