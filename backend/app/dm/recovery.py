"""Explicit recovery of failed adjudication before any player-visible output."""
import logging
import uuid

from sqlalchemy import select
from models.campaigns import Campaign
from models.dm import DmTurn, DmTurnAttempt, DMStream

logger = logging.getLogger(__name__)


def retry_failed_adjudication(db, campaign_id, turn_id, attempt_id):
    """Create one fresh attempt for the original input; caller owns commit.

    Lock campaign before turn, matching normal coordination. A replay naming
    the old attempt returns its replacement, even with a different command key.
    Partial-stream continuation remains a separate recovery operation.
    """
    campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update()
                          .execution_options(populate_existing=True)).scalars().one()
    turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id, DmTurn.campaign_id == campaign_id).with_for_update()
                      .execution_options(populate_existing=True)).scalars().first()
    if turn is None:
        raise ValueError("Turn not found")
    old = db.get(DmTurnAttempt, attempt_id)
    if old is None or old.turn_id != turn.id:
        raise ValueError("Attempt not found")
    replacement = db.execute(select(DmTurnAttempt).where(
        DmTurnAttempt.turn_id == turn.id, DmTurnAttempt.parent_attempt_id == old.id,
    ).order_by(DmTurnAttempt.attempt_number)).scalars().first()
    if old.status == "abandoned" and old.abandonment_reason == "explicit_retry" and replacement is not None:
        return turn, replacement
    if turn.current_attempt_id != old.id or turn.status != "failed_visible" or old.status != "failed_visible":
        raise ValueError("Only the current failed attempt can be retried")
    stream = db.get(DMStream, old.stream_id) if old.stream_id else None
    if old.streaming_started_at or (stream and stream.chunk_count):
        raise ValueError("A partial narration requires stream recovery")
    from app.dm.turns import _now
    now = _now()
    old.status = "abandoned"
    old.abandoned_at = now
    old.abandonment_reason = "explicit_retry"
    if stream:
        stream.status = "abandoned"
        stream.abandoned_at = now
        stream.abandonment_reason = "explicit_retry"
    attempt = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, campaign_id=campaign_id,
        thread_id=turn.thread_id, audience=turn.audience,
        attempt_number=old.attempt_number + 1, parent_attempt_id=old.id,
        status="prepared", source_revision=campaign.revision,
        input_set_revision=turn.input_set_revision,
        submission_ids=list(old.submission_ids), roll_evidence=list(old.roll_evidence or []),
        assembly_window_start=old.assembly_window_start,
        assembly_window_end=old.assembly_window_end,
    )
    db.add(attempt)
    db.flush()
    turn.current_attempt_id = attempt.id
    turn.status = "pending"
    turn.source_revision = campaign.revision
    turn.streaming_attempt_id = None
    turn.streaming_started_at = None
    db.flush()
    return turn, attempt


def execute_committed_attempt(attempt_id):
    """Best-effort post-response execution; prepared work stays sweepable."""
    from database import SessionLocal
    from app.dm.execution import execute_dm_attempt
    try:
        if SessionLocal is not None:
            with SessionLocal() as db:
                execute_dm_attempt(db, uuid.UUID(str(attempt_id)))
    except Exception:
        logger.exception("post-response DM execution failed attempt_id=%s", attempt_id)
