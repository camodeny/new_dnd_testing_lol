"""Transactional outbox service — issue #190."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from models import Outbox

logger = logging.getLogger(__name__)

PENDING = "pending"
CLAIMED = "claimed"
PUBLISHED = "published"
FAILED = "failed"


def enqueue_outbox(
    db: Session,
    *,
    event_type: str,
    payload: dict | None = None,
    aggregate_type: str = "campaign",
    aggregate_id: uuid.UUID | str | None = None,
    campaign_id: uuid.UUID | str | None = None,
    operation_id: str | None = None,
    outbox_id: uuid.UUID | None = None,
    commit: bool = True,
) -> Outbox:
    if not event_type or not event_type.strip():
        raise ValueError("event_type is required")
    # Normalize UUIDs
    if isinstance(aggregate_id, str):
        try:
            aggregate_id = uuid.UUID(aggregate_id)
        except ValueError:
            pass
    if isinstance(campaign_id, str):
        campaign_id = uuid.UUID(campaign_id)
    rec = Outbox(
        id=outbox_id or uuid.uuid4(),
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id if isinstance(aggregate_id, uuid.UUID) else None,
        campaign_id=campaign_id,
        event_type=event_type.strip(),
        operation_id=operation_id,
        payload=payload,
        status=PENDING,
        attempts=0,
    )
    db.add(rec)
    db.flush()
    if commit:
        db.commit()
        db.refresh(rec)
    logger.info("outbox enqueued id=%s type=%s op=%s campaign=%s", rec.id, event_type, operation_id or "-", campaign_id or "-")
    return rec


def claim_outbox_batch(
    db: Session,
    *,
    batch_size: int = 10,
    claimed_by: str = "relay",
    lease_seconds: int = 300,
    commit: bool = True,
) -> list[Outbox]:
    """Safe claim: selects pending or expired claimed records, marks claimed."""
    now = datetime.now(timezone.utc)
    lease_cutoff = now - timedelta(seconds=lease_seconds)
    # Find candidates: pending, or failed (retryable), or claimed but expired
    candidates = db.execute(
        select(Outbox)
        .where(
            or_(
                Outbox.status == PENDING,
                Outbox.status == FAILED,
                and_(Outbox.status == CLAIMED, or_(Outbox.claimed_at == None, Outbox.claimed_at < lease_cutoff)),  # noqa
            ),
            or_(Outbox.next_attempt_at == None, Outbox.next_attempt_at <= now),  # noqa
        )
        .order_by(Outbox.created_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    claimed = []
    for rec in candidates:
        rec.status = CLAIMED
        rec.claimed_at = now
        rec.claimed_by = claimed_by
        rec.attempts = (rec.attempts or 0) + 1
        rec.updated_at = now
        claimed.append(rec)
    if claimed:
        db.flush()
        if commit:
            db.commit()
            for r in claimed:
                db.refresh(r)
        logger.info("outbox claimed %s records by %s", len(claimed), claimed_by)
    return claimed


def ack_published(db: Session, outbox_id: uuid.UUID, *, commit: bool = True) -> Outbox | None:
    rec = db.get(Outbox, outbox_id)
    if rec is None:
        return None
    # Idempotent ack: already published is ok (duplicate publication safe)
    if rec.status == PUBLISHED:
        logger.info("outbox ack already published id=%s", outbox_id)
        return rec
    rec.status = PUBLISHED
    rec.published_at = datetime.now(timezone.utc)
    rec.last_error = None
    db.flush()
    if commit:
        db.commit()
        db.refresh(rec)
    logger.info("outbox published id=%s type=%s", outbox_id, rec.event_type)
    return rec


def mark_failed(
    db: Session,
    outbox_id: uuid.UUID,
    error: str,
    *,
    retry_delay_seconds: int = 60,
    commit: bool = True,
) -> Outbox | None:
    rec = db.get(Outbox, outbox_id)
    if rec is None:
        return None
    rec.status = FAILED
    rec.last_error = error[:2000] if error else None
    rec.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)
    rec.claimed_at = None
    rec.claimed_by = None
    db.flush()
    if commit:
        db.commit()
        db.refresh(rec)
    logger.warning("outbox failed id=%s error=%s", outbox_id, error[:200])
    return rec


def release_claim(db: Session, outbox_id: uuid.UUID, *, commit: bool = True) -> Outbox | None:
    """Release a claimed record back to pending (e.g. relay crash before publish)."""
    rec = db.get(Outbox, outbox_id)
    if rec is None:
        return None
    if rec.status == CLAIMED:
        rec.status = PENDING
        rec.claimed_at = None
        rec.claimed_by = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(rec)
    return rec


def recover_expired_claims(db: Session, *, lease_seconds: int = 300, commit: bool = True) -> int:
    """Sweeper: reset expired claimed records to pending for retry."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=lease_seconds)
    result = db.execute(
        update(Outbox)
        .where(Outbox.status == CLAIMED, Outbox.claimed_at < cutoff)
        .values(status=PENDING, claimed_at=None, claimed_by=None)
    )
    if result.rowcount:
        if commit:
            db.commit()
        logger.info("outbox sweeper recovered %s expired claims", result.rowcount)
    return result.rowcount or 0


def get_outbox_metrics(db: Session) -> dict:
    """Backlog depth, unpublished age, attempts."""
    from sqlalchemy import func

    now = datetime.now(timezone.utc)
    rows = db.execute(select(Outbox.status, func.count()).group_by(Outbox.status)).all()
    by_status = {k: v for k, v in rows}
    oldest = db.execute(
        select(func.min(Outbox.created_at)).where(Outbox.status != PUBLISHED)
    ).scalar()
    if oldest is not None and oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=timezone.utc)
    age_s = (now - oldest).total_seconds() if oldest else 0
    max_attempts = db.execute(select(func.max(Outbox.attempts)).where(Outbox.status != PUBLISHED)).scalar() or 0
    return {
        "backlog_by_status": by_status,
        "unpublished_age_seconds": age_s,
        "max_attempts": max_attempts,
        "total_unpublished": sum(v for k, v in by_status.items() if k != PUBLISHED),
    }


def list_unpublished(db: Session, *, limit: int = 100) -> list[Outbox]:
    return list(
        db.execute(
            select(Outbox).where(Outbox.status != PUBLISHED).order_by(Outbox.created_at.asc()).limit(limit)
        )
        .scalars()
        .all()
    )


def commit_with_outbox(
    db: Session,
    *,
    event_type: str,
    payload: dict | None = None,
    aggregate_type: str = "campaign",
    aggregate_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    operation_id: str | None = None,
    commit: bool = True,
    mutate: callable | None = None,
) -> Outbox:
    """Enqueue outbox atomically with optional mutate callback in same transaction.

    If mutate raises, outbox is rolled back too.
    """
    if mutate is not None:
        mutate()
    return enqueue_outbox(
        db,
        event_type=event_type,
        payload=payload,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        campaign_id=campaign_id,
        operation_id=operation_id,
        commit=commit,
    )
