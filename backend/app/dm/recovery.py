"""Explicit recovery of failed turns — issue #208.

Two operations on the same spine:

- :func:`retry_failed_adjudication` — explicit Retry from the original
  accepted player intent. Abandons the failed attempt (including partial
  visible streams), discards staged effects, and starts a fresh logical
  attempt from current authoritative state. Idempotent.
- :func:`retry_narration_only` — narration-independent retry that reuses a
  preserved valid ``contract_snapshot`` without re-adjudication.
- Partial-stream resume/continuation lives in ``app.dm.narration``
  (``resume_narration_stream`` / ``continue_partial_stream``).
"""
import logging
import uuid

from sqlalchemy import select
from models.campaigns import Campaign
from models.dm import DmTurn, DmTurnAttempt, DMStream

logger = logging.getLogger(__name__)


def _fresh_attempt_from_old(db, *, campaign, turn, old, reuse_contract: bool = False):
    """Build the fresh logical attempt; caller owns flush/commit."""
    from app.dm.turns import _now

    now = _now()
    # Abandon the failed attempt: staged effects stay on the old row for
    # audit but are never promoted (commit_operation_id is attempt-scoped,
    # so the fresh attempt cannot duplicate prior staged effects).
    old.status = "abandoned"
    old.abandoned_at = now
    old.abandonment_reason = "explicit_retry"
    if old.stream_id:
        stream = db.get(DMStream, old.stream_id)
        if stream is not None and stream.status != "abandoned":
            stream.status = "abandoned"
            stream.abandoned_at = now
            stream.abandonment_reason = "explicit_retry"
    attempt = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, campaign_id=campaign.id,
        thread_id=turn.thread_id, audience=turn.audience,
        attempt_number=old.attempt_number + 1, parent_attempt_id=old.id,
        status="prepared", source_revision=campaign.revision,
        input_set_revision=turn.input_set_revision,
        submission_ids=list(old.submission_ids or []),
        roll_evidence=list(old.roll_evidence or []),
        assembly_window_start=old.assembly_window_start,
        assembly_window_end=old.assembly_window_end,
        # Fresh logical attempt: new idempotency scope, no staged effects
        # carried over (re-staged on success), no stream attached.
        staged_effects=[],
        contract_snapshot=dict(old.contract_snapshot)
        if (reuse_contract and old.contract_snapshot) else None,
        commit_operation_id=None,
        retry_count=0,
        next_retry_at=None,
        last_error=None,
        error_class=None,
    )
    # Fresh idempotency scope for the new logical attempt.
    attempt.commit_operation_id = str(attempt.id)
    db.add(attempt)
    db.flush()
    turn.current_attempt_id = attempt.id
    turn.status = "pending"
    turn.source_revision = campaign.revision
    turn.streaming_attempt_id = None
    turn.streaming_started_at = None
    db.flush()
    logger.info(
        "dm_retry explicit_retry turn_id=%s old_attempt_id=%s new_attempt_id=%s "
        "reuse_contract=%s abandoned_partial_stream=%s",
        turn.id, old.id, attempt.id, reuse_contract,
        bool(old.stream_id),
    )
    return turn, attempt


def retry_failed_adjudication(db, campaign_id, turn_id, attempt_id):
    """Create one fresh attempt for the original input; caller owns commit.

    Explicit Retry semantics (#208): abandons the failed attempt, discards
    staged effects (never promoted — commit is attempt-scoped), and starts
    a fresh logical attempt from the same original accepted player intent
    (``submission_ids``/``roll_evidence``) at the current authoritative
    campaign revision. Recovery AI runs are non-billable (ledger
    classification), so no double-charge for the discarded work.

    Works for pre-visibility AND partial-stream failures: a persisted
    partial stream is abandoned (never canonical) rather than blocking
    retry. Use ``resume_narration_stream``/``continue_partial_stream`` when
    the goal is to keep the visible prefix instead of starting fresh.

    Lock campaign before turn, matching normal coordination. A replay naming
    the old attempt returns its replacement, even with a different command key.
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
    # Explicit Retry abandons even partial visible streams — the old stream
    # is marked abandoned/non-canonical and the fresh attempt starts clean.
    return _fresh_attempt_from_old(db, campaign=campaign, turn=turn, old=old)


def retry_narration_only(db, campaign_id, turn_id, attempt_id):
    """Fresh attempt reusing the preserved valid contract (no re-adjudication).

    For narration-only failures where ``contract_snapshot`` survived: the
    new attempt carries the snapshot forward so the executor can narrate
    and commit without calling the adjudication model again. Staged effects
    are still re-staged on success (never copied as committed truth).
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
    if turn.current_attempt_id != old.id or turn.status != "failed_visible" or old.status != "failed_visible":
        raise ValueError("Only the current failed attempt can be retried")
    if not old.contract_snapshot:
        raise ValueError("No preserved structured result — use full explicit Retry")
    return _fresh_attempt_from_old(
        db, campaign=campaign, turn=turn, old=old, reuse_contract=True
    )


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
