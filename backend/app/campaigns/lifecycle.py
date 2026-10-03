"""Campaign lifecycle transitions — lobby/starting/active/archived (#240, #265).

Archive is dormancy, not deletion: a status-only transition that preserves
all campaign/world/character/thread state. Restore returns the campaign to
its pre-archive status (derived from the authoritative archive event), so
archiving can never bypass start eligibility.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.campaigns.events import commit_campaign_mutation, has_domain_event, latest_domain_event
from app.campaigns.service import (
    CampaignCommandError,
    compute_start_eligibility,
    is_archived,
    validate_lifecycle_transition,
)
from models.campaigns import Campaign, CampaignMember
from models.dm import DmTurn, DmTurnAttempt
from models.threads import CampaignThread

logger = logging.getLogger(__name__)

ARCHIVED_EVENT = "campaign.lifecycle.archived"
RESTORABLE_STATUSES = ("lobby", "starting", "active")


def restore_target(db: Session, campaign: Campaign) -> str | None:
    """Pre-archive status a restore returns to (only set while archived).

    Derived server-side from the authoritative archive event, never
    reconstructed from the truncated events feed.
    """
    if not is_archived(campaign):
        return None
    archive_event = latest_domain_event(db, campaign.id, ARCHIVED_EVENT)
    prior = (archive_event.payload or {}).get("from") if archive_event else None
    return prior if prior in RESTORABLE_STATUSES else None


def _require_startable(db: Session, locked: Campaign, actor_id: uuid.UUID) -> None:
    members = db.execute(
        select(CampaignMember).where(CampaignMember.campaign_id == locked.id)
    ).scalars().all()
    eligibility = compute_start_eligibility(locked, list(members), db)
    if not eligibility["eligible"]:
        logger.warning(
            "campaign start blocked campaign_id=%s actor_id=%s blockers=%s",
            locked.id, actor_id, eligibility["blockers"],
        )
        raise CampaignCommandError(status_code=409, detail={
            "message": "Campaign is not ready to start",
            "blockers": eligibility["blockers"],
        })


def _require_not_streaming(db: Session, locked: Campaign, actor_id: uuid.UUID) -> None:
    """Never strand visible output mid-stream (#265).

    Archive is rejected while a DM attempt is running or streaming, or its
    turn is streaming. Prepared and pending work simply defers and resumes
    after restore. Stuck running claims do not block forever:
    recover_stuck_attempts resets expired claims to prepared (only after
    proving the executor is dead via the advisory execution lock).
    """
    streaming_turn = db.execute(
        select(DmTurn.id).where(
            DmTurn.campaign_id == locked.id,
            DmTurn.status == "streaming",
        ).limit(1)
    ).scalars().first()
    inflight_attempt = db.execute(
        select(DmTurnAttempt.id).where(
            DmTurnAttempt.campaign_id == locked.id,
            DmTurnAttempt.status.in_(("running", "streaming")),
        ).limit(1)
    ).scalars().first()
    if inflight_attempt is not None or streaming_turn is not None:
        logger.warning(
            "campaign archive rejected campaign_id=%s actor_id=%s reason=dm_streaming",
            locked.id, actor_id,
        )
        raise CampaignCommandError(status_code=409, detail="A DM turn is currently streaming; retry archive shortly")


def _require_restore_target(db: Session, locked: Campaign, target: str, actor_id: uuid.UUID) -> None:
    prior = restore_target(db, locked)
    if prior is None:
        logger.warning(
            "campaign restore rejected campaign_id=%s actor_id=%s reason=unknown_pre_archive_status",
            locked.id, actor_id,
        )
        raise CampaignCommandError(status_code=409, detail="Campaign cannot be restored: pre-archive status unknown")
    if target != prior:
        logger.warning(
            "campaign restore rejected campaign_id=%s actor_id=%s to=%s pre_archive=%s",
            locked.id, actor_id, target, prior,
        )
        raise CampaignCommandError(status_code=409, detail=f"Restore returns the campaign to its pre-archive status ({prior})")


def transition_lifecycle(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    target_raw,
    expected_revision: int,
    operation_id: str,
) -> dict:
    """Revision-guarded lifecycle transition (flush-only; caller commits).

    A duplicate command landing on the already-correct state converges
    without a revision bump or new event; restores only converge when the
    campaign was actually archived before.
    """
    current = db.get(Campaign, campaign_id)
    requested = str(target_raw or "").strip().lower()
    if current is not None and requested == str(current.status or "").lower():
        if requested == "archived" or has_domain_event(db, campaign_id, ARCHIVED_EVENT):
            logger.info(
                "campaign lifecycle duplicate campaign_id=%s actor_id=%s status=%s revision=%s",
                campaign_id, actor_id, current.status, current.revision,
            )
            return {"campaign": current.to_dict(), "converged": True, "duplicate": True}

    def _mutate(locked: Campaign):
        try:
            target = validate_lifecycle_transition(locked.status, target_raw)
        except ValueError as exc:
            logger.warning(
                "campaign lifecycle transition rejected campaign_id=%s actor_id=%s from=%s to=%s",
                locked.id, actor_id, locked.status, target_raw,
            )
            raise CampaignCommandError(status_code=409, detail=str(exc)) from exc
        if target == "starting":
            _require_startable(db, locked, actor_id)
        if target == "archived":
            _require_not_streaming(db, locked, actor_id)
        if is_archived(locked):
            # Status-only reactivation: same campaign ID/world/canon — no
            # reseed, no duplicate clocks/NPCs/threads/characters.
            _require_restore_target(db, locked, target, actor_id)
        locked.status = target

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type=f"campaign.lifecycle.{requested or 'invalid'}",
        operation_id=operation_id,
        actor_id=actor_id,
        payload={"from": current.status if current else None, "to": requested},
        mutate=_mutate,
        commit=False,
    )
    logger.info(
        "campaign lifecycle transitioned campaign_id=%s actor_id=%s status=%s revision=%s",
        campaign_id, actor_id, campaign_after.status, campaign_after.revision,
    )
    return {"campaign": campaign_after.to_dict(), "event": event.to_dict()}


def _archived_duration_seconds(db: Session, campaign_id: uuid.UUID) -> float | None:
    """Wall-clock seconds since the latest archive event (observability only).

    Fictional time is frozen while archived, so this never advances
    fictional clocks or NPC plans.
    """
    try:
        event = latest_domain_event(db, campaign_id, ARCHIVED_EVENT)
        if event is None or event.created_at is None:
            return None
        archived_at = event.created_at
        if archived_at.tzinfo is None:
            archived_at = archived_at.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - archived_at).total_seconds())
    except Exception as exc:
        logger.warning("campaign archived duration unavailable campaign_id=%s error=%s", campaign_id, exc)
        return None


def _verify_restored_projection(db: Session, campaign_id: uuid.UUID) -> str:
    """Read-only post-restore check that live-table snapshot inputs survived.

    Never mutates: the restored state is already committed and
    reconnect/snapshot recovers the read model, so failures are only logged.
    """
    try:
        shared = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        threads = db.scalar(
            select(func.count()).select_from(CampaignThread).where(CampaignThread.campaign_id == campaign_id)
        ) or 0
        members = db.scalar(
            select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
        ) or 0
        if shared is None:
            return "degraded:shared_thread_missing"
        return f"ok:threads={threads}:members={members}"
    except Exception as exc:
        logger.warning("campaign restore projection verify failed campaign_id=%s error=%s", campaign_id, exc)
        return "unknown:verify_failed"


def log_lifecycle_outcome(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    source_status: str,
    target: str,
    result: dict,
) -> None:
    """Archive/restore observability after the transition committed."""
    after = result.get("campaign", {}) if isinstance(result, dict) else {}
    if isinstance(result, dict) and result.get("converged"):
        logger.info(
            "campaign lifecycle duplicate campaign_id=%s actor_id=%s status=%s revision=%s",
            campaign_id, actor_id, after.get("status"), after.get("revision"),
        )
        return
    if target == "archived":
        logger.info(
            "campaign archived campaign_id=%s actor_id=%s from=%s to=%s revision=%s",
            campaign_id, actor_id, source_status, after.get("status"), after.get("revision"),
        )
    elif source_status == "archived":
        logger.info(
            "campaign restored campaign_id=%s actor_id=%s from=archived to=%s revision=%s "
            "duration_archived_s=%s projection_restoration=%s",
            campaign_id, actor_id, after.get("status"), after.get("revision"),
            _archived_duration_seconds(db, campaign_id), _verify_restored_projection(db, campaign_id),
        )
