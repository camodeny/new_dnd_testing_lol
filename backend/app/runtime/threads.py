"""Thread identity and audience authorization — issue #195.

Centralized, reusable checks for all live-table history reads/writes.
Privacy is enforced as a data property, not a frontend filter.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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


def get_campaign_thread(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID
) -> CampaignThread | None:
    return (
        db.execute(
            select(CampaignThread).where(
                CampaignThread.id == thread_id,
                CampaignThread.campaign_id == campaign_id,
            )
        )
        .scalars()
        .first()
    )


def is_thread_member(db: Session, thread_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return (
        db.get(CampaignThreadMember, {"thread_id": thread_id, "user_id": user_id})
        is not None
    )


def get_or_create_campaign_thread(
    db: Session, campaign_id: uuid.UUID, *, created_by: uuid.UUID | None = None
) -> CampaignThread:
    """Return the durable shared thread, creating it if needed.

    Does NOT commit — transaction ownership stays at the request boundary.
    Concurrency is enforced by a partial unique index
    (uq_campaign_threads_one_campaign_per_campaign). A concurrent creation
    that wins the race raises IntegrityError on flush, which we catch and
    re-resolve to the winner.
    """
    existing = (
        db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id,
                CampaignThread.thread_type == "campaign",
            )
        )
        .scalars()
        .first()
    )
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
    # Isolate the insert/race recovery so a benign shared-thread race does not
    # discard unrelated pending work in the caller's session. Flush any
    # pre-existing pending changes outside the savepoint first.
    if db.new or db.dirty or db.deleted:
        # Flush unrelated pending work outside the race savepoint; this makes
        # it part of the outer transaction and immune to the savepoint rollback.
        db.flush()
    try:
        with db.begin_nested():
            db.add(thread)
            db.flush()
    except IntegrityError:
        # Savepoint rolled back automatically; outer transaction and its
        # unrelated pending work remain intact.
        winner = (
            db.execute(
                select(CampaignThread).where(
                    CampaignThread.campaign_id == campaign_id,
                    CampaignThread.thread_type == "campaign",
                )
            )
            .scalars()
            .first()
        )
        if winner is not None:
            return winner
        raise
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
        campaign_id,
        thread.id,
        len(all_ids),
    )
    return thread


def get_or_create_private_gameplay_thread(
    db: Session,
    *,
    campaign_id: uuid.UUID,
    created_by: uuid.UUID,
    private_kind: str,
    participant_ids: list[uuid.UUID],
    title: str,
) -> tuple[CampaignThread, bool]:
    """Return one durable AI-DM or direct thread for a logical audience.

    The database-backed private key makes retries and concurrent creation
    converge on the same thread. The AI DM is the sole DM and is represented
    by the ``dm`` thread kind, not by a human/profile membership row.
    """
    if private_kind not in ("dm", "direct"):
        raise ValueError("private_kind must be 'dm' or 'direct'")

    all_ids = set(participant_ids) | {created_by}
    if private_kind == "dm" and all_ids != {created_by}:
        raise ValueError("AI-DM threads have exactly one player participant")
    if private_kind == "direct" and len(all_ids) != 2:
        raise ValueError("Direct threads require exactly two player participants")

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ThreadNotFoundError("Campaign not found")
    for uid in all_ids:
        if campaign.owner_id != uid and not is_campaign_member(db, campaign_id, uid):
            raise ThreadAuthorizationError(f"User {uid} is not a campaign member")

    ordered_ids = sorted(str(uid) for uid in all_ids)
    private_key = f"{private_kind}:{':'.join(ordered_ids)}"
    existing = (
        db.execute(
            select(CampaignThread).where(
                CampaignThread.campaign_id == campaign_id,
                CampaignThread.private_key == private_key,
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        if not is_thread_member(db, existing.id, created_by):
            raise ThreadAuthorizationError("Not authorized to access this thread")
        logger.info(
            "thread get_or_create reused campaign_id=%s thread_id=%s private_kind=%s user_id=%s",
            campaign_id,
            existing.id,
            private_kind,
            created_by,
        )
        return existing, False

    thread = CampaignThread(
        campaign_id=campaign_id,
        thread_type="private",
        private_kind=private_kind,
        private_key=private_key,
        title=title,
        created_by=created_by,
    )
    # Flush unrelated pending work outside the savepoint so a concurrent
    # private-key race does not discard it (mirrors shared-thread fix).
    if db.new or db.dirty or db.deleted:
        db.flush()
    try:
        with db.begin_nested():
            db.add(thread)
            db.flush()
    except IntegrityError:
        winner = (
            db.execute(
                select(CampaignThread).where(
                    CampaignThread.campaign_id == campaign_id,
                    CampaignThread.private_key == private_key,
                )
            )
            .scalars()
            .first()
        )
        if winner is None:
            raise
        if not is_thread_member(db, winner.id, created_by):
            raise ThreadAuthorizationError("Not authorized to access this thread")
        return winner, False

    for uid in all_ids:
        db.add(CampaignThreadMember(thread_id=thread.id, user_id=uid, role="member"))
    db.flush()
    logger.info(
        "thread created campaign_id=%s thread_id=%s private_kind=%s member_count=%s",
        campaign_id,
        thread.id,
        private_kind,
        len(all_ids),
    )
    return thread, True


def can_read_thread(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """Centralized read-authorization check.

    - Shared ``campaign`` threads: any campaign member (including owner) may read.
    - Private threads: only explicit thread members may read.  Owner/admin status
      alone does NOT grant access — this is the critical privacy invariant.
    - Missing thread or ambiguous state → deny (fail closed).
    """
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        logger.info(
            "thread read denied campaign_id=%s thread_id=%s reason=not_found",
            campaign_id,
            thread_id,
        )
        return False
    if thread.thread_type == "campaign":
        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return False
        authorized = campaign.owner_id == user_id or is_campaign_member(
            db, campaign_id, user_id
        )
        if not authorized:
            logger.info(
                "thread read denied campaign_id=%s thread_id=%s reason=not_campaign_member",
                campaign_id,
                thread_id,
            )
        return authorized
    if thread.thread_type == "private":
        authorized = is_thread_member(db, thread_id, user_id)
        if not authorized:
            logger.info(
                "thread read denied campaign_id=%s thread_id=%s user_id=%s reason=not_thread_member",
                campaign_id,
                thread_id,
                user_id,
            )
        return authorized
    logger.info(
        "thread read denied campaign_id=%s thread_id=%s reason=unknown_thread_type",
        campaign_id,
        thread_id,
    )
    return False


def can_write_thread(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    # Writes reuse read authorization — if you cannot read, you cannot write.
    return can_read_thread(db, campaign_id, thread_id, user_id)


def _hide_private_not_found(thread: CampaignThread | None, user_id: uuid.UUID) -> bool:
    """True if this is a private thread existence that must be hidden (→ 404)."""
    return thread is not None and thread.thread_type == "private"


def assert_can_read_thread(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID
) -> CampaignThread:
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        raise ThreadNotFoundError("Thread not found")
    if not can_read_thread(db, campaign_id, thread_id, user_id):
        if _hide_private_not_found(thread, user_id):
            logger.info(
                "thread read denied campaign_id=%s thread_id=%s user_id=%s reason=not_thread_member hide_as_not_found",
                campaign_id,
                thread_id,
                user_id,
            )
            raise ThreadNotFoundError("Thread not found")
        raise ThreadAuthorizationError("Not authorized to access this thread")
    return thread


def assert_can_write_thread(
    db: Session, campaign_id: uuid.UUID, thread_id: uuid.UUID, user_id: uuid.UUID
) -> CampaignThread:
    thread = get_campaign_thread(db, campaign_id, thread_id)
    if thread is None:
        raise ThreadNotFoundError("Thread not found")
    if not can_write_thread(db, campaign_id, thread_id, user_id):
        if _hide_private_not_found(thread, user_id):
            logger.info(
                "thread write denied campaign_id=%s thread_id=%s user_id=%s reason=not_thread_member hide_as_not_found",
                campaign_id,
                thread_id,
                user_id,
            )
            raise ThreadNotFoundError("Thread not found")
        raise ThreadAuthorizationError("Not authorized to write to this thread")
    return thread


def list_threads_for_user(
    db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID
) -> list[CampaignThread]:
    """Return only threads the user is authorized to see — hidden thread metadata is not leaked."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return []
    is_member = campaign.owner_id == user_id or is_campaign_member(
        db, campaign_id, user_id
    )
    if not is_member:
        return []
    all_threads = (
        db.execute(
            select(CampaignThread)
            .where(CampaignThread.campaign_id == campaign_id)
            .order_by(CampaignThread.created_at)
        )
        .scalars()
        .all()
    )
    visible: list[CampaignThread] = []
    for thread in all_threads:
        if thread.thread_type == "campaign":
            visible.append(thread)
        elif thread.thread_type == "private" and is_thread_member(
            db, thread.id, user_id
        ):
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
