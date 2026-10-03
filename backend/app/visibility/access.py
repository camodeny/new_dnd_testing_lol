"""Campaign participation, DM authority, and per-record receive checks.

Fail-closed everywhere: missing/ambiguous visibility, unknown records,
non-membership, and revoked/missing grants all deny with a reason code.
Access is never inferred from a related shared record — each record is
authorized from its own visibility and its own active grants. Owner status
grants ``dm_only`` access (the AI-DM authority lane) but never ``private``
access: private disclosure requires an explicit active grant naming the
human user.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.world._common import coerce_uuid
from app.visibility.policy import MEMBER_VISIBILITIES, canonical_visibility
from models.campaigns import Campaign, CampaignMember
from models.world import (
    GRANT_TARGET_KINDS,
    WorldEntity,
    WorldFact,
    WorldKnowledge,
    WorldRelation,
    WorldVisibilityGrant,
)

logger = logging.getLogger(__name__)


def is_world_authority(campaign: Campaign, user_id: Any) -> bool:
    """Owner / DM authority sees restricted records; ordinary members do not."""
    return campaign.owner_id == user_id


def is_campaign_participant(db: Session, campaign: Campaign, user_id: Any) -> bool:
    """Owner or member of ``campaign``."""
    if campaign.owner_id == user_id:
        return True
    return db.get(CampaignMember, {"campaign_id": campaign.id, "user_id": user_id}) is not None


def validate_grant_target_kind(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s not in GRANT_TARGET_KINDS:
        raise ValueError(f"target_kind must be one of {sorted(GRANT_TARGET_KINDS)}")
    return s


def load_grant_target(db: Session, target_kind: str, target_id: uuid.UUID):
    if target_kind == "fact":
        return db.get(WorldFact, target_id)
    if target_kind == "relation":
        return db.get(WorldRelation, target_id)
    if target_kind == "entity":
        return db.get(WorldEntity, target_id)
    if target_kind == "knowledge":
        return db.get(WorldKnowledge, target_id)
    return None


def has_active_grant(
    db: Session, campaign_id: uuid.UUID, target_kind: str,
    target_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    return db.execute(
        select(func.count()).select_from(WorldVisibilityGrant).where(
            WorldVisibilityGrant.campaign_id == campaign_id,
            WorldVisibilityGrant.target_kind == target_kind,
            WorldVisibilityGrant.target_id == target_id,
            WorldVisibilityGrant.grantee_user_id == user_id,
            WorldVisibilityGrant.revoked_at.is_(None),
        )
    ).scalar_one() > 0


def may_user_receive(
    db: Session, campaign: Campaign, target_kind: str, target_id: Any,
    user_id: Any,
) -> dict[str, Any]:
    """May human U receive record R?

    Only R's own visibility + R's own active grants count. Unknown or
    ambiguous state denies.
    """
    try:
        kind = validate_grant_target_kind(target_kind)
        tid = coerce_uuid(target_id, field="target_id")
        uid = coerce_uuid(user_id, field="user_id")
    except ValueError:
        return {"allowed": False, "reason": "record_not_found"}
    if not is_campaign_participant(db, campaign, uid):
        structured_log(
            logger, logging.INFO, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind, reason="not_campaign_member",
        )
        return {"allowed": False, "reason": "not_campaign_member"}
    record = load_grant_target(db, kind, tid)
    if record is None or getattr(record, "campaign_id", None) != campaign.id:
        return {"allowed": False, "reason": "record_not_found"}
    try:
        vis = canonical_visibility(getattr(record, "visibility", None))
    except ValueError:
        structured_log(
            logger, logging.WARNING, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind, reason="ambiguous_visibility",
        )
        return {"allowed": False, "reason": "ambiguous_visibility"}
    if vis in MEMBER_VISIBILITIES:
        return {"allowed": True, "reason": "member_visible"}
    if vis == "dm_only":
        if is_world_authority(campaign, uid):
            return {"allowed": True, "reason": "dm_authority"}
        structured_log(
            logger, logging.INFO, "world_access_denied",
            campaign_id=str(campaign.id), target_kind=kind,
            reason="dm_only_requires_authority",
        )
        return {"allowed": False, "reason": "dm_only_requires_authority"}
    # Private: exactly the active grantee set — owner included only if granted.
    if has_active_grant(db, campaign.id, kind, tid, uid):
        return {"allowed": True, "reason": "explicit_grant"}
    structured_log(
        logger, logging.INFO, "world_access_denied",
        campaign_id=str(campaign.id), target_kind=kind, reason="private_requires_grant",
    )
    return {"allowed": False, "reason": "private_requires_grant"}
