"""Thread identity and audience authorization — issue #195.

Centralized, reusable checks for all live-table history reads/writes.
Privacy is enforced as a data property, not a frontend filter.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.service import is_campaign_member
from models import Campaign, CampaignThread, CampaignThreadMember

logger = logging.getLogger(__name__)


class ThreadAuthorizationError(PermissionError):
    pass


class ThreadNotFoundError(LookupError):
    pass


def parse_thread_id(thread_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(thread_id))
    except Exception as exc:
        raise ThreadNotFoundError("Invalid thread id") from exc


def get_thread(db: Session, thread_id: uuid.UUID) -> CampaignThread | None:
    return db.get(CampaignThread, thread_id)


def get_campaign_thread(db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID) -> CampaignThread | None:
    return db.execute(
        select(CampaignThread).where(
            CampaignThread.id == thread_id,
            CampaignThread.campaign_id == campaign_id,
        )
    ).scalars().first()


def is_thread_member(db: Session, thread_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        db.get(CampaignThreadMember, {"thread_id": thread_id, "user_id": user_id}) is not None
    )


def get_or_create_campaign_thread(
    db: Session, campaign_id: uuid.UUID, *, created_by: uuid.UUID | None = None
) -> CampaignThread:
    existing = db.execute(
        select(CampaignThread).where(
            CampaignThread.campaign_id == campaign_id,
            CampaignThread.thread_type == "campaign",
        )
    ).scalars().first()
    if existing is not None:
        return existing
    # Create shared thread — id is durable and survives reconnects
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ThreadNotFoundError("Campaign not found")
    thread = CampaignThread(
        campaign_id=campaign_id,
        thread_type="campaign",
        title="Campaign",
        created_by=created_by or campaign.owner_id,
    )
    db.add(thread)
    db.flush()
    # Flush alone does not survive session close when no outer commit follows
    # (e.g. pure list call). Commit here so the durable id survives reconnects.
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Re-query after race — another request may have created it concurrently
        existing = db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id,
                CampaignThread.thread_type == "campaign",
            )
        ).scalars().first()
        if existing is not None:
            return existing
        raise
    # Re-attach after commit for continued use in same request session
    db.refresh(thread)
    return thread


def resolve_thread_id(
    db: Session,
    campaign_id: uuid.UUID,
    raw_thread_id: str | None,
    *,
    created_by: uuid.UUID | None = None,
) -> uuid.UUID:
    """Resolve client-supplied thread identifier to a durable UUID.

    - ``None`` / ``""`` / ``"main"`` → shared campaign thread (created lazily)
    - UUID string → validated private or shared thread belonging to campaign
    - Anything else → ThreadNotFoundError (fails closed)
    """
    if not raw_thread_id or raw_thread_id == "main":
        return get_or_create_campaign_thread(db, campaign_id, created_by=created_by).id
    thread_uuid = parse_thread_id(raw_thread_id)
    thread = get_campaign_thread(db, campaign_id, thread_uuid)
    if thread is None:
        raise ThreadNotFoundError("Thread not found")
    return thread.id


def create_private_thread(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    created_by: uuid.UUID,
    member_ids: list[uuid.UUID],
    title: str | None = None,
) -> CampaignThread:
    # Validate campaign membership for all participants (including creator)
    all_ids = set(member_ids) | {created_by}
    for uid in all_ids:
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            raise ThreadNotFoundError("Campaign not found")
        # Owner counts as member implicitly for campaign membership check,
        # but private access still requires explicit thread membership.
        if campaign.owner_id != uid and not is_campaign_member(db, campaign_id, uid):
            raise ThreadAuthorizationError(f"User {uid} is not a campaign member")
    thread = CampaignThread(
        campaign_id=campaign_id,
        thread_type="private",
        title=title,
        created_by=created_by,
    )
    db.add(thread)
    db.flush()
    for uid in all_ids:
        db.add(CampaignThreadMember(thread_id=thread.id, user_id=uid, role="member"))
    db.flush()
    logger.info(
        "thread created campaign_id=%s thread_id=%s thread_type=private member_count=%s",
        campaign_id, thread.id, len(all_ids),
    )
    return thread


def can_read_thread(db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Centralized read-authorization check.

    - Shared ``campaign`` threads: any campaign member (including owner) may read.
    - Private threads: only explicit thread members may read.  Owner/admin status
      alone does NOT grant access — this is the critical privacy invariant.
    - Missing thread or ambiguous state → deny (fail closed).
    """
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        logger.info("thread read denied campaign_id=%s thread_id=%s reason=not_found", campaign_id, thread_id)
        return False
    if thread.thread_type == "campaign":
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return False
        authorized = campaign.owner_id == user_id or is_campaign_member(db, campaign_id, user_id)
        if not authorized:
            logger.info("thread read denied campaign_id=%s thread_id=%s reason=not_campaign_member", campaign_id, thread_id)
        return authorized
    if thread.thread_type == "private":
        authorized = is_thread_member(db, thread_id, user_id)
        if not authorized:
            logger.info(
                "thread read denied campaign_id=%s thread_id=%s user_id=%s reason=not_thread_member",
                campaign_id, thread_id, user_id,
            )
        return authorized
    logger.info("thread read denied campaign_id=%s thread_id=%s reason=unknown_thread_type", campaign_id, thread_id)
    return False


def can_write_thread(db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    # Writes reuse read authorization — if you cannot read, you cannot write.
    return can_read_thread(db, campaign_id, thread_id, user_id)


def assert_can_read_thread(db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID) -> CampaignThread:
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        raise ThreadNotFoundError("Thread not found")
    if not can_read_thread(db, campaign_id, thread_id, user_id):
        raise ThreadAuthorizationError("Not authorized to access this thread")
    return thread


def assert_can_write_thread(db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID) -> CampaignThread:
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        raise ThreadNotFoundError("Thread not found")
    if not can_write_thread(db, campaign_id, thread_id, user_id):
        raise ThreadAuthorizationError("Not authorized to write to this thread")
    return thread


def list_threads_for_user(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> list[CampaignThread]:
    """Return only threads the user is authorized to see — hidden thread metadata is not leaked."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return []
    is_member = campaign.owner_id == user_id or is_campaign_member(db, campaign_id, user_id)
    if not is_member:
        return []
    all_threads = db.execute(
        select(CampaignThread).where(CampaignThread.campaign_id == campaign_id).order_by(CampaignThread.created_at)
    ).scalars().all()
    visible: list[CampaignThread] = []
    for thread in all_threads:
        if thread.thread_type == "campaign":
            visible.append(thread)
        elif thread.thread_type == "private" and is_thread_member(db, thread.id, user_id):
            visible.append(thread)
    return visible


def remove_thread_member(db: Session, thread_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    member = db.get(CampaignThreadMember, {"thread_id": thread_id, "user_id": user_id})
    if member is None:
        return False
    db.delete(member)
    db.flush()
    logger.info("thread membership revoked thread_id=%s user_id=%s", thread_id, user_id)
    return True
