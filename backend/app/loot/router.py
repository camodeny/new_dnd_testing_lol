"""Loot box endpoints — issue #463."""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, run_campaign_command
from app.deps.idempotency import require_idempotency_key
from app.loot.service import LootError, box_view, open_loot_box
from database import get_db
from models.campaigns import Campaign

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/campaigns/{campaign_id}/loot-boxes/{box_id}/open")
def post_open_loot_box(
    box_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for()),
    db: Session = Depends(get_db),
):
    """The character's player opens a sealed box; code draws what is inside."""
    try:
        box_uuid = uuid.UUID(box_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Loot box not found") from exc
    key = require_idempotency_key(request, payload.get("operation_id"))

    def execute():
        try:
            box, _ = open_loot_box(db, campaign, box_uuid, actor_id=profile.id)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LootError as exc:
            message = str(exc)
            raise HTTPException(status_code=404 if "not found" in message else 409, detail=message) from exc
        return {"loot_box": box_view(box)}

    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=key,
        command_type="loot_box.open", scope_type="loot_box", scope_id=box_uuid,
        payload=payload, execute=execute,
    )
