"""Settings → Backup & restore (#870): the setup as a file, the daily
snapshots, and a restore that previews first (services/backup.py). Admins only."""
import json
import logging
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    AutoPlaylistToggle, ListSubscription, LiveChannel, Provider, ProviderCategory, RecordingRule, TagRule,
    TentacleUser, get_db,
)
from routers.auth import require_admin
from services import backup as B

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(require_admin)])


def _split(value: Optional[str]) -> Optional[list]:
    if value is None:
        return None
    return [v for v in (s.strip() for s in value.split(",")) if v]


@router.get("")
def overview(db: Session = Depends(get_db)):
    """The parts with what this install has of each, its providers and
    people (the backup form), and the snapshots."""
    counts = {
        "vod_categories": db.query(func.count(ProviderCategory.id)).filter(ProviderCategory.whitelisted == True).scalar(),  # noqa: E712
        "live_channels": db.query(func.count(LiveChannel.id)).filter(LiveChannel.enabled == True).scalar(),  # noqa: E712
        "home_rows": None,
        "playlists": (db.query(func.count(TagRule.id)).scalar() or 0)
        + (db.query(func.count(AutoPlaylistToggle.id)).filter(AutoPlaylistToggle.enabled == True).scalar() or 0),  # noqa: E712
        "lists": db.query(func.count(ListSubscription.id)).scalar(),
        "recording_rules": db.query(func.count(RecordingRule.id)).scalar(),
    }
    return {
        "parts": [{"id": i, "label": label, "provider": prov, "count": counts.get(i)} for i, label, prov in B.PARTS],
        "providers": [{"id": p.id, "name": p.name} for p in db.query(Provider).order_by(Provider.id)],
        "people": [{"id": u.id, "name": u.display_name} for u in db.query(TentacleUser).order_by(TentacleUser.id)],
        "snapshots": B.snapshots(),
        "keep": B.KEEP,
        "folder": B.backup_dir(),
    }


@router.get("/export")
def export_file(parts: Optional[str] = Query(None), people: Optional[str] = Query(None),
                providers: Optional[str] = Query(None), db: Session = Depends(get_db)):
    """The file to download: the parts picked (comma-separated ids), for
    the people and providers picked (names; none = all)."""
    data = B.export(db, parts=_split(parts), people=_split(people), providers=_split(providers))
    name = f"tentacle-setup-{datetime.utcnow():%Y-%m-%d}.json"
    return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/snapshots")
def back_up_now(db: Session = Depends(get_db)):
    return B.snapshot(db, "now")


@router.get("/snapshots/{name}")
def download_snapshot(name: str):
    try:
        data = B.read_snapshot(name)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "No such backup")
    return Response(json.dumps(data, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


class RestoreBody(BaseModel):
    backup: Optional[dict] = None       # an uploaded file's content
    snapshot: Optional[str] = None      # or one of the snapshots
    parts: Optional[List[str]] = None
    people: Optional[List[str]] = None
    providers: Optional[Dict[str, Optional[int]]] = None   # file provider name -> provider id here, null = skip
    replace: List[str] = []


def _file(body: RestoreBody) -> dict:
    try:
        if body.snapshot:
            return B.read_snapshot(body.snapshot)
        return B.check_file(body.backup)
    except FileNotFoundError:
        raise HTTPException(404, "No such backup")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/preview")
def preview(body: RestoreBody, db: Session = Depends(get_db)):
    """What the restore would do; writes nothing."""
    data = _file(body)
    out = B.plan(db, data, parts=body.parts, people=body.people, providers=body.providers,
                 replace=body.replace, apply=False)
    out["file"] = B.summary(data)
    out["left_out"] = data.get("left_out") or []
    return out


@router.post("/restore")
def restore(body: RestoreBody, db: Session = Depends(get_db)):
    """Restore what the preview showed (the setup as it was is saved first)."""
    data = _file(body)
    try:
        out = B.plan(db, data, parts=body.parts, people=body.people, providers=body.providers,
                     replace=body.replace, apply=True)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    out["file"] = B.summary(data)
    return out
