"""Shared campaign authorization — issue #335.

Single home for the member/owner checks previously copy-pasted across the
dm/runtime/realtime/dm_streams/rolls routers. No behavior change.
"""

import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member, parse_campaign_id
from models.campaigns import Campaign


def authorized_campaign(db: Session, campaign_id: str, user_id: uuid.UUID) -> Campaign:
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Invalid campaign id") from exc
    campaign = db.get(Campaign, cid)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != user_id and not is_campaign_member(db, cid, user_id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return campaign


def require_owner(campaign: Campaign, user_id: uuid.UUID):
    if campaign.owner_id != user_id:
        raise HTTPException(status_code=403, detail="Only the campaign owner can perform this DM lifecycle action")
