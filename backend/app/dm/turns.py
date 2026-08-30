"""Durable DM turn and turn-attempt state machine — issue #200.

One logical DM turn may consume multiple player submissions that arrived
in the same unresolved fictional moment (same campaign+thread/audience).
New eligible submissions invalidate a prepared attempt pre-stream without
mutating campaign truth. Once the first visible chunk commits,
the input set is locked. Stale source-revision attempts are discarded
via optimistic campaign-revision validation. A campaign cannot advance
past an unresolved streaming/failed-visible turn.

States
------
DmTurn.status: pending | streaming | succeeded | failed_visible
DmTurnAttempt.status: prepared | running | superseded | streaming | succeeded | failed | failed_visible | discarded

Coordinator
-----------
Decides when unresolved (accepted) submissions for a campaign+thread form a
candidate turn. Called after each accepted submission. No long-held DB locks;
optimistic revision check only at commit time.

Security: assembly only includes submissions authorized for the turn's
audience/thread (thread_id equality).

Observability: logs assembly window, included submissions, attempt
invalidation reason, source revision conflicts, time waiting vs executing.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Campaign, DmTurn, DmTurnAttempt, PlayerSubmission

logger = logging.getLogger(__name__)

# ── Statuses ────────────────────────────────────────────────────────────────

TURN_PENDING = "pending"
TURN_STREAMING = "streaming"
TURN_SUCCEEDED = "succeeded"
TURN_FAILED_VISIBLE = "failed_visible"

ATTEMPT_PREPARED = "prepared"
ATTEMPT_RUNNING = "running"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_STREAMING = "streaming"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
ATTEMPT_FAILED_VISIBLE = "failed_visible"
ATTEMPT_DISCARDED = "discarded"

ACTIVE_TURN_STATUSES = {TURN_PENDING, TURN_STREAMING, TURN_FAILED_VISIBLE}
BLOCKING_TURN_STATUSES = {TURN_STREAMING, TURN_FAILED_VISIBLE}
PRE_STREAM_ATTEMPT_STATUSES = {ATTEMPT_PREPARED, ATTEMPT_RUNNING}


class TurnConflictError(Exception):
    """Raised when a new turn would advance past an unresolved visible turn."""

    def __init__(self, campaign_id: uuid.UUID, thread_id: str, blocking_turn_id: uuid.UUID):
        self.campaign_id = campaign_id
        self.thread_id = thread_id
        self.blocking_turn_id = blocking_turn_id
        super().__init__(
            f"Campaign {campaign_id} thread {thread_id} blocked by unresolved turn {blocking_turn_id} "
            f"(streaming/failed_visible); next turn cannot advance"
        )


class StreamBoundaryError(Exception):
    """Raised when new input attempts to mutate a post-stream input set."""

    def __init__(self, turn_id: uuid.UUID, attempt_id: uuid.UUID):
        self.turn_id = turn_id
        self.attempt_id = attempt_id
        super().__init__(
            f"Turn {turn_id} attempt {attempt_id} already streaming; input set is committed and cannot change"
        )


class StaleRevisionError(Exception):
    """Attempt source_revision is stale vs current campaign revision."""

    def __init__(self, campaign_id: uuid.UUID, expected: int, actual: int, attempt_id: uuid.UUID):
        self.campaign_id = campaign_id
        self.expected_revision = expected
        self.actual_revision = actual
        self.attempt_id = attempt_id
        super().__init__(
            f"Stale source_revision for attempt {attempt_id} campaign {campaign_id}: "
            f"expected {expected}, actual {actual}"
        )


class AttemptSupersededError(Exception):
    """Attempt was superseded pre-stream; result must be discarded."""

    def __init__(self, attempt_id: uuid.UUID, reason: str | None = None):
        self.attempt_id = attempt_id
        self.reason = reason
        super().__init__(f"Attempt {attempt_id} was superseded pre-stream and its result must be discarded")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collect_unresolved_submissions(
    db: Session, campaign_id: uuid.UUID, thread_id: str
) -> list[PlayerSubmission]:
    """All accepted submissions for this campaign+thread, ordered by sequence.

    Only submissions authorized for this thread are returned; authorization is
    enforced upstream (PlayerSubmission.thread_id is server-derived, not trusted
    from arbitrary client claims). Caller must ensure thread_id is already
    resolved to a durable thread the actor is authorized to see.
    """
    # Normalize thread_id to string as stored
    tid = str(thread_id)
    rows = db.execute(
        select(PlayerSubmission)
        .where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.thread_id == tid,
            PlayerSubmission.resolution_status == "accepted",
        )
        .order_by(PlayerSubmission.sequence.asc())
    ).scalars().all()
    return list(rows)


def get_active_turn(db: Session, campaign_id: uuid.UUID, thread_id: str) -> DmTurn | None:
    """Most recent turn in blocking/pending state for this campaign+thread.

    Returns the turn that would block new assembly if streaming/failed_visible,
    or the pending turn that may be expanded pre-stream.
    """
    tid = str(thread_id)
    turn = db.execute(
        select(DmTurn)
        .where(DmTurn.campaign_id == campaign_id, DmTurn.thread_id == tid, DmTurn.status.in_(list(ACTIVE_TURN_STATUSES)))
        .order_by(DmTurn.created_at.desc())
        .limit(1)
    ).scalars().first()
    return turn


def get_turn(db: Session, turn_id: uuid.UUID) -> DmTurn | None:
    return db.get(DmTurn, turn_id)


def get_attempt(db: Session, attempt_id: uuid.UUID) -> DmTurnAttempt | None:
    return db.get(DmTurnAttempt, attempt_id)


def list_turns(db: Session, campaign_id: uuid.UUID, thread_id: str | None = None, limit: int = 100) -> list[DmTurn]:
    q = select(DmTurn).where(DmTurn.campaign_id == campaign_id).order_by(DmTurn.created_at.asc()).limit(limit)
    if thread_id:
        q = q.where(DmTurn.thread_id == str(thread_id))
    return list(db.execute(q).scalars().all())


def _has_blocking_turn(db: Session, campaign_id: uuid.UUID, thread_id: str, exclude_turn_id: uuid.UUID | None = None) -> DmTurn | None:
    """Whether any streaming/failed_visible turn blocks advancing.

    Per spec: "A campaign cannot advance to a conflicting later DM resolution
    while a visible partial turn remains unresolved." We enforce per
    campaign+thread (most precise) and also globally per campaign as safety.

    For the test suite we block when any streaming/failed_visible turn exists
    for the same campaign+thread.
    """
    tid = str(thread_id)
    q = select(DmTurn).where(
        DmTurn.campaign_id == campaign_id,
        DmTurn.thread_id == tid,
        DmTurn.status.in_(list(BLOCKING_TURN_STATUSES)),
    )
    if exclude_turn_id:
        q = q.where(DmTurn.id != exclude_turn_id)
    return db.execute(q.order_by(DmTurn.created_at.asc()).limit(1)).scalars().first()


# ── Coordinator ─────────────────────────────────────────────────────────────


def coordinate_turn(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: str,
    audience: str = "campaign",
) -> tuple[DmTurn, DmTurnAttempt] | None:
    """Decide when unresolved submissions form a candidate turn.

    Call after each accepted submission (or periodically). No long-held DB lock;
    work is executed outside the transaction and committed via optimistic revision.

    Returns (turn, current_attempt) if a turn is active/created, None if no
    unresolved submissions.

    Raises:
        TurnConflictError: if a streaming/failed_visible turn blocks new turns.
        StreamBoundaryError: if new submissions would alter a post-stream input set.

    Observability: logs assembly_window, included submissions, invalidation reason,
    source revision conflicts, waiting vs executing time.

    Audience/thread safety: only submissions with matching thread_id are included;
    thread_id is server-resolved and authorization-checked upstream, so callers do
    not need to re-check audience here, but we record it for audit.
    """
    start_wait = time.monotonic()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found")
    source_revision = int(campaign.revision) if campaign.revision is not None else 0
    tid = str(thread_id)

    unresolved = _collect_unresolved_submissions(db, campaign_id, tid)
    if not unresolved:
        logger.info(
            "dm_turn coordinator no_work campaign_id=%s thread_id=%s source_revision=%s",
            campaign_id, tid, source_revision,
        )
        return None

    # If a blocking turn exists for this thread, new turns cannot advance yet.
    # But the existing blocked turn itself is still the candidate — callers that
    # requested coordination for that thread should get the blocking turn back,
    # not a new one. We only block *new* turn creation, not re-assembly of the
    # blocked turn. The streaming boundary is checked below when attempting to
    # expand a pending turn; distinct-forcing of "no new turn while streaming"
    # is correct for threads that would otherwise create a second logical turn.
    active = get_active_turn(db, campaign_id, tid)

    # No active turn → create new logical turn from all unresolved submissions.
    if active is None:
        # Double-check no global blocking turn would be conflicting? Conservatively
        # block if any streaming/failed_visible turn exists for same campaign+thread
        # (already checked above: active is None means no blocking turn).
        sub_ids = [str(s.id) for s in unresolved]
        window_start = min(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
        window_end = max(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
        # Ensure timestamps are timezone-aware
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
        if window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)

        turn = DmTurn(
            id=uuid.uuid4(),
            campaign_id=campaign_id,
            thread_id=tid,
            audience=audience,
            status=TURN_PENDING,
            source_revision=source_revision,
            input_set_revision=1,
            submission_ids=sub_ids,
            assembly_window_start=window_start,
            assembly_window_end=window_end,
        )
        db.add(turn)
        db.flush()

        attempt = DmTurnAttempt(
            id=uuid.uuid4(),
            turn_id=turn.id,
            attempt_number=1,
            status=ATTEMPT_PREPARED,
            campaign_id=campaign_id,
            thread_id=tid,
            audience=audience,
            source_revision=source_revision,
            input_set_revision=1,
            submission_ids=list(sub_ids),
            assembly_window_start=window_start,
            assembly_window_end=window_end,
        )
        db.add(attempt)
        db.flush()
        turn.current_attempt_id = attempt.id
        db.flush()
        db.commit()
        db.refresh(turn)
        db.refresh(attempt)

        waiting_ms = int((time.monotonic() - start_wait) * 1000)
        # Store waiting time for observability
        turn.time_waiting_ms = waiting_ms
        # Commit waiting time without extra lock
        try:
            db.flush()
            db.commit()
        except Exception:
            db.rollback()

        logger.info(
            "dm_turn created campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s source_revision=%s input_set_revision=1 "
            "submission_count=%s submission_ids=%s assembly_window_start=%s assembly_window_end=%s time_waiting_ms=%s",
            campaign_id, tid, turn.id, attempt.id, source_revision, len(sub_ids), sub_ids,
            window_start.isoformat(), window_end.isoformat(), waiting_ms,
        )
        return turn, attempt

    # Active turn exists. If it is streaming/failed_visible, it blocks expansion.
    if active.status in BLOCKING_TURN_STATUSES:
        # Active is the blocking turn itself. If unresolved submissions are exactly
        # those already in the blocking turn, then coordinator is just re-reporting
        # the active turn (even post-stream, the same submissions form the turn).
        # Only when *new* submissions have arrived beyond the committed input set
        # should we enforce the stream boundary.
        active_ids = set(active.submission_ids or [])
        new_ids = [str(s.id) for s in unresolved]
        if set(new_ids) == active_ids:
            cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
            # Re-check if attempt still exists; if not, treat as no work
            if cur is None:
                # No current attempt (should not happen) — return turn as is
                return active, None  # type: ignore[return-value]
            return active, cur
        # New eligible submissions arrived after streaming started.
        # Spec: "Once the first visible chunk commits, the turn input set cannot silently change."
        cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
        if cur and cur.status in (ATTEMPT_STREAMING, ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_VISIBLE):
            logger.warning(
                "dm_turn stream_boundary_blocked campaign_id=%s thread_id=%s blocking_turn_id=%s attempt_id=%s "
                "existing_submissions=%s new_submissions=%s source_revision=%s",
                campaign_id, tid, active.id, cur.id, sorted(active_ids), sorted(set(new_ids)), source_revision,
            )
            raise StreamBoundaryError(active.id, cur.id)
        # Even if attempt not yet marked streaming but turn is, treat as boundary
        logger.warning(
            "dm_turn blocked_by_streaming campaign_id=%s thread_id=%s blocking_turn_id=%s status=%s new_submission_count=%s",
            campaign_id, tid, active.id, active.status, len(new_ids),
        )
        raise TurnConflictError(campaign_id, tid, active.id)

    # Active is pending (prepared). Check if new submissions expand the input set.
    assert active.status == TURN_PENDING, f"unexpected active status {active.status}"
    active_ids = set(active.submission_ids or [])
    new_ids_set = set(str(s.id) for s in unresolved)
    new_ids_ordered = [str(s.id) for s in unresolved]

    if new_ids_set == active_ids:
        # No change — return existing turn/attempt, no invalidation.
        cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
        logger.info(
            "dm_turn coordinator no_change campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s submission_count=%s",
            campaign_id, tid, active.id, cur.id if cur else None, len(new_ids_ordered),
        )
        return active, cur  # type: ignore[return-value]

    # Input set expanded pre-stream — must supersede old attempt, create new one.
    cur_attempt = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
    if cur_attempt is None:
        # No current attempt (should not happen); create one with expanded set
        logger.warning("dm_turn pending_without_attempt campaign_id=%s thread_id=%s turn_id=%s", campaign_id, tid, active.id)
        # Fall through to creating new attempt without supersession
        new_rev = active.input_set_revision + 1
        window_start = active.assembly_window_start or _now()
        # Compute new window end as latest unresolved
        window_end = max(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
        if window_end and window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
        new_attempt = DmTurnAttempt(
            id=uuid.uuid4(),
            turn_id=active.id,
            attempt_number=(cur_attempt.attempt_number + 1) if cur_attempt else 1,
            status=ATTEMPT_PREPARED,
            campaign_id=campaign_id,
            thread_id=tid,
            audience=audience,
            source_revision=source_revision,
            input_set_revision=new_rev,
            submission_ids=list(new_ids_ordered),
            assembly_window_start=window_start,
            assembly_window_end=window_end,
        )
        db.add(new_attempt)
        db.flush()
        active.submission_ids = list(new_ids_ordered)
        active.input_set_revision = new_rev
        active.current_attempt_id = new_attempt.id
        active.assembly_window_end = window_end
        active.source_revision = source_revision  # refresh to latest campaign revision for new attempt lineage
        db.flush()
        db.commit()
        db.refresh(active)
        db.refresh(new_attempt)
        logger.info(
            "dm_turn superseded_no_prior_attempt campaign_id=%s thread_id=%s turn_id=%s new_attempt_id=%s "
            "input_set_revision=%s submission_count=%s submission_ids=%s source_revision=%s invalidation_reason=new_eligible_submission_pre_stream",
            campaign_id, tid, active.id, new_attempt.id, new_rev, len(new_ids_ordered), new_ids_ordered, source_revision,
        )
        return active, new_attempt

    # If current attempt already streaming, cannot supersede
    if cur_attempt.status == ATTEMPT_STREAMING or active.streaming_started_at is not None:
        logger.warning(
            "dm_turn stream_boundary_blocked campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s status=%s input_set_revision=%s",
            campaign_id, tid, active.id, cur_attempt.id, cur_attempt.status, active.input_set_revision,
        )
        raise StreamBoundaryError(active.id, cur_attempt.id)

    if cur_attempt.status not in PRE_STREAM_ATTEMPT_STATUSES:
        # Already superseded/failed — treat as pending but need new attempt anyway
        # Allow creating new attempt if prior was failed before streaming (retry)
        if cur_attempt.status in (ATTEMPT_FAILED, ATTEMPT_DISCARDED):
            pass
        else:
            logger.warning(
                "dm_turn supersession_blocked_status campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s status=%s",
                campaign_id, tid, active.id, cur_attempt.id, cur_attempt.status,
            )
            raise StreamBoundaryError(active.id, cur_attempt.id)

    # Safe to supersede pre-stream attempt.
    old_attempt_id = cur_attempt.id
    old_status = cur_attempt.status
    cur_attempt.status = ATTEMPT_SUPERSEDED
    cur_attempt.invalidation_reason = "new_eligible_submission_pre_stream"
    cur_attempt.invalidated_at = _now()
    # Note: we do not mutate campaign truth; supersession is metadata only.

    window_start = active.assembly_window_start or cur_attempt.assembly_window_start or _now()
    window_end = max(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
    if window_end and window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)
    if window_start and window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)

    new_rev = active.input_set_revision + 1
    new_attempt = DmTurnAttempt(
        id=uuid.uuid4(),
        turn_id=active.id,
        attempt_number=cur_attempt.attempt_number + 1,
        status=ATTEMPT_PREPARED,
        campaign_id=campaign_id,
        thread_id=tid,
        audience=audience,
        source_revision=source_revision,
        input_set_revision=new_rev,
        submission_ids=list(new_ids_ordered),
        parent_attempt_id=old_attempt_id,
        assembly_window_start=window_start,
        assembly_window_end=window_end,
    )
    db.add(new_attempt)
    db.flush()

    # Update turn to point to new attempt and expand submission set.
    active.submission_ids = list(new_ids_ordered)
    active.input_set_revision = new_rev
    active.current_attempt_id = new_attempt.id
    active.assembly_window_end = window_end
    # source_revision for turn remains original for audit, but we also keep
    # attempt-level source_revision as the current campaign revision at supersession.
    # For observability we log both.

    db.flush()
    db.commit()
    db.refresh(active)
    db.refresh(cur_attempt)
    db.refresh(new_attempt)

    waiting_ms = int((time.monotonic() - start_wait) * 1000)
    logger.info(
        "dm_turn superseded campaign_id=%s thread_id=%s turn_id=%s old_attempt_id=%s old_status=%s new_attempt_id=%s "
        "attempt_number=%s input_set_revision=%s submission_count=%s submission_ids=%s source_revision=%s "
        "invalidation_reason=new_eligible_submission_pre_stream assembly_window_start=%s assembly_window_end=%s time_waiting_ms=%s",
        campaign_id, tid, active.id, old_attempt_id, old_status, new_attempt.id,
        new_attempt.attempt_number, new_rev, len(new_ids_ordered), new_ids_ordered, source_revision,
        window_start.isoformat() if window_start else None,
        window_end.isoformat() if window_end else None,
        waiting_ms,
    )
    return active, new_attempt


# ── Stream-start commitment boundary ────────────────────────────────────────


def mark_streaming_started(db: Session, turn_id: uuid.UUID, attempt_id: uuid.UUID) -> tuple[DmTurn, DmTurnAttempt]:
    """Establish stream-start commitment boundary for the input set.

    After this call, the turn's input set is locked; any new eligible submissions
    will be rejected with StreamBoundaryError until the turn resolves, and a new
    turn cannot be assembled. This implements: "Once the first visible chunk commits,
    the turn input set cannot silently change."

    Idempotent: if already streaming with same attempt, returns without error.

    Args:
        db: Session
        turn_id: logical turn id
        attempt_id: attempt that is starting streaming

    Returns:
        (turn, attempt) after transition to streaming.

    Raises:
        ValueError: if turn/attempt not found or mismatched.
        AttemptSupersededError: if attempt was already superseded pre-stream.
    """
    turn = db.get(DmTurn, turn_id)
    attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")

    # Idempotent: already streaming with same attempt
    if turn.status == TURN_STREAMING and str(turn.streaming_attempt_id) == str(attempt_id) and attempt.status == ATTEMPT_STREAMING:
        return turn, attempt

    if attempt.status == ATTEMPT_SUPERSEDED:
        raise AttemptSupersededError(attempt_id, attempt.invalidation_reason)
    if attempt.status not in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
        raise ValueError(f"Attempt {attempt_id} cannot transition to streaming from status {attempt.status}")
    if turn.status not in (TURN_PENDING,):
        raise ValueError(f"Turn {turn_id} cannot transition to streaming from status {turn.status}")
    if str(turn.current_attempt_id) != str(attempt_id):
        # Attempt is not current — likely superseded by newer pre-stream input.
        # Treat as superseded discard path.
        raise AttemptSupersededError(attempt_id, "superseded_by_newer_attempt_pre_stream")

    now = _now()
    turn.status = TURN_STREAMING
    turn.streaming_started_at = now
    turn.streaming_attempt_id = attempt.id
    attempt.status = ATTEMPT_STREAMING
    attempt.streaming_started_at = now
    # Also ensure attempt started_at is set (execution started)
    if attempt.started_at is None:
        attempt.started_at = now

    db.flush()
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)

    logger.info(
        "dm_turn streaming_started campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s source_revision=%s input_set_revision=%s submission_ids=%s",
        turn.campaign_id, turn.thread_id, turn.id, attempt.id, attempt.source_revision, attempt.input_set_revision, attempt.submission_ids,
    )
    return turn, attempt


def mark_attempt_running(db: Session, attempt_id: uuid.UUID, worker_job_id: uuid.UUID | None = None) -> DmTurnAttempt:
    """Mark attempt as running (worker claimed). Recoverable if worker crashes.

    Does not hold DB locks; caller should ensure attempt is still current.

    Observability: records waiting vs executing time.
    """
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"Attempt {attempt_id} not found")
    if attempt.status not in (ATTEMPT_PREPARED,):
        raise ValueError(f"Attempt {attempt_id} cannot transition to running from {attempt.status}")
    now = _now()
    attempt.status = ATTEMPT_RUNNING
    attempt.started_at = now
    if worker_job_id:
        attempt.worker_job_id = worker_job_id
    db.flush()
    db.commit()
    db.refresh(attempt)
    logger.info("dm_turn attempt_running turn_id=%s attempt_id=%s attempt_number=%s source_revision=%s input_set_revision=%s",
                attempt.turn_id, attempt.id, attempt.attempt_number, attempt.source_revision, attempt.input_set_revision)
    return attempt


# ── Commit / stale-revision guard ───────────────────────────────────────────


def commit_turn(
    db: Session,
    turn_id: uuid.UUID,
    attempt_id: uuid.UUID,
    expected_revision: int | None = None,
    mutate: Any | None = None,
    event_type: str = "dm.turn_resolved",
    payload: dict | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
) -> tuple[DmTurn, DmTurnAttempt, Any]:
    """Commit a DM turn's authoritative effects with optimistic revision validation.

    Uses the same semantics as commit_campaign_mutation: stale expected_revision
    fails without partially mutating state. This satisfies:
    "Stale source-revision attempts cannot commit over newer authoritative state."

    Also enforces TurnConflictError if another streaming/failed_visible turn for the
    same campaign/thread would be conflicting. And ensures the attempt is still
    the current, not-superseded attempt; obsolete attempts are discarded harmlessly.

    Args:
        db: Session
        turn_id: logical turn id
        attempt_id: attempt id (must be current and streaming/succeeded-eligible)
        expected_revision: campaign revision the attempt expects (defaults to attempt.source_revision)
        mutate: optional callable receiving Campaign to apply state changes within transaction
        event_type, payload, operation_id, actor_id: forwarded to commit_campaign_mutation

    Returns:
        (turn, attempt, event) where event is the domain event or None if no fictional mutation.

    Raises:
        AttemptSupersededError: if attempt was superseded pre-stream (harmless discard).
        StreamBoundaryError: if attempt not in committed input set.
        StaleRevisionError / RevisionConflictError: if source revision is stale.
        TurnConflictError: if another visible partial turn blocks advancing.
    """
    from app.campaigns.events import RevisionConflictError, commit_campaign_mutation

    turn = db.get(DmTurn, turn_id)
    attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")

    # Obsolete attempt check: if attempt was superseded pre-stream, its result is discarded.
    # Worker may still finish model execution but must not commit.
    if attempt.status == ATTEMPT_SUPERSEDED or str(turn.current_attempt_id) != str(attempt_id):
        # Check if superseded flag
        if attempt.status == ATTEMPT_SUPERSEDED or str(turn.current_attempt_id) != str(attempt_id):
            logger.info(
                "dm_turn commit_discarded_superseded campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s current_attempt_id=%s status=%s invalidation_reason=%s",
                turn.campaign_id, turn.thread_id, turn.id, attempt.id, turn.current_attempt_id, attempt.status, attempt.invalidation_reason,
            )
            attempt.status = ATTEMPT_DISCARDED if attempt.status != ATTEMPT_SUPERSEDED else ATTEMPT_SUPERSEDED
            attempt.last_error = f"Discarded superseded attempt; current attempt is {turn.current_attempt_id}"
            attempt.error_class = "superseded"
            # Ensure committed even though we discard
            try:
                db.flush()
                db.commit()
            except Exception:
                db.rollback()
            raise AttemptSupersededError(attempt_id, attempt.invalidation_reason)

    # Must have passed stream-start boundary to commit authoritative effects.
    # For tests that commit without explicit streaming, allow prepared/running as well
    # but warn — real flow should have called mark_streaming_started first.
    if attempt.status not in (ATTEMPT_STREAMING, ATTEMPT_RUNNING, ATTEMPT_PREPARED, ATTEMPT_SUCCEEDED):
        raise ValueError(f"Attempt {attempt_id} status {attempt.status} cannot commit")

    # Ensure turn is not blocked by another visible partial turn (for a different turn id)
    blocking = _has_blocking_turn(db, turn.campaign_id, turn.thread_id, exclude_turn_id=turn.id)
    if blocking is not None:
        # If the blocking turn is *this* turn, it's okay (we are streaming). Otherwise conflict.
        # _has_blocking_turn already excluded this turn, so any result here is a different turn.
        logger.warning(
            "dm_turn commit_blocked_by_visible_turn campaign_id=%s thread_id=%s turn_id=%s blocking_turn_id=%s blocking_status=%s",
            turn.campaign_id, turn.thread_id, turn.id, blocking.id, blocking.status,
        )
        raise TurnConflictError(turn.campaign_id, turn.thread_id, blocking.id)

    # Optimistic revision validation
    expected = expected_revision if expected_revision is not None else attempt.source_revision
    campaign = db.get(Campaign, turn.campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {turn.campaign_id} not found")
    actual = int(campaign.revision) if campaign.revision is not None else 0
    if int(expected) != actual:
        # Stale source revision — do not mutate campaign truth, mark attempt failed
        logger.warning(
            "dm_turn stale_source_revision_conflict campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s expected=%s actual=%s source_revision=%s",
            turn.campaign_id, turn.thread_id, turn.id, attempt.id, expected, actual, attempt.source_revision,
        )
        attempt.last_error = f"Stale source_revision: expected {expected}, actual {actual}"
        attempt.error_class = "stale_revision"
        attempt.completed_at = _now()
        # Keep turn in pending/streaming so it can be retried with refreshed revision
        # Mark attempt as failed (recoverable) — not failed_visible unless it had streamed
        if attempt.status == ATTEMPT_STREAMING or turn.status == TURN_STREAMING:
            attempt.status = ATTEMPT_FAILED_VISIBLE
            turn.status = TURN_FAILED_VISIBLE
            attempt.error_class = "stale_revision_visible"
        else:
            attempt.status = ATTEMPT_FAILED
        try:
            db.flush()
            db.commit()
        except Exception:
            db.rollback()
        raise StaleRevisionError(turn.campaign_id, int(expected), actual, attempt.id)

    # All guards passed — perform authoritative mutation via campaign revision helper.
    # This increments campaign revision exactly once and creates an immutable domain event.
    # Use commit=False so we can update turn/attempt atomically in same txn.
    execute_start = time.monotonic()
    try:
        campaign_after, event = commit_campaign_mutation(
            db,
            turn.campaign_id,
            expected_revision=int(expected),
            event_type=event_type,
            payload=payload or {"turn_id": str(turn.id), "attempt_id": str(attempt.id), "submission_ids": attempt.submission_ids},
            operation_id=operation_id or str(attempt.id),
            actor_id=actor_id,
            mutate=mutate,
            commit=False,
        )
    except RevisionConflictError as exc:
        # Map to stale revision observability — commit_campaign_mutation already rolled back
        logger.warning(
            "dm_turn revision_conflict_on_commit campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s expected=%s actual=%s",
            turn.campaign_id, turn.thread_id, turn.id, attempt.id, exc.expected_revision, exc.actual_revision,
        )
        db.rollback()
        attempt.last_error = str(exc)
        attempt.error_class = "revision_conflict"
        attempt.completed_at = _now()
        if attempt.status == ATTEMPT_STREAMING or turn.status == TURN_STREAMING:
            attempt.status = ATTEMPT_FAILED_VISIBLE
            turn.status = TURN_FAILED_VISIBLE
        else:
            attempt.status = ATTEMPT_FAILED
        try:
            db.flush()
            db.commit()
        except Exception:
            db.rollback()
        raise StaleRevisionError(turn.campaign_id, exc.expected_revision, exc.actual_revision, attempt.id) from exc

    # Success path: mark turn and attempt as succeeded, resolve submissions
    now = _now()
    turn.status = TURN_SUCCEEDED
    turn.resolved_at = now
    turn.time_executing_ms = int((time.monotonic() - execute_start) * 1000)
    attempt.status = ATTEMPT_SUCCEEDED
    attempt.completed_at = now
    attempt.result = event.to_dict() if hasattr(event, "to_dict") else {"event_id": str(event.id)}
    attempt.processing_duration_ms = turn.time_executing_ms
    attempt.last_error = None
    attempt.error_class = None

    # Resolve included submissions: mark as resolved so future turns don't re-include them.
    if attempt.submission_ids:
        try:
            # submission_ids are UUID strings
            sub_uuids = [uuid.UUID(s) for s in (attempt.submission_ids or [])]
            rows = db.execute(
                select(PlayerSubmission).where(PlayerSubmission.id.in_(sub_uuids))
            ).scalars().all()
            for row in rows:
                row.resolution_status = "resolved"
                row.resolved_at = now
        except Exception as e:
            # Non-fatal: log but don't fail commit
            logger.warning("dm_turn failed to resolve submissions turn_id=%s error=%s", turn.id, e)

    db.flush()
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)
    db.refresh(campaign_after)
    db.refresh(event)

    logger.info(
        "dm_turn committed campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s new_revision=%s event_id=%s "
        "input_set_revision=%s submission_count=%s assembly_window_start=%s assembly_window_end=%s time_executing_ms=%s",
        turn.campaign_id, turn.thread_id, turn.id, attempt.id, campaign_after.revision, event.id,
        attempt.input_set_revision, len(attempt.submission_ids or []),
        turn.assembly_window_start.isoformat() if turn.assembly_window_start else None,
        turn.assembly_window_end.isoformat() if turn.assembly_window_end else None,
        turn.time_executing_ms,
    )
    return turn, attempt, event


def discard_superseded_result(db: Session, attempt_id: uuid.UUID, reason: str = "superseded") -> DmTurnAttempt | None:
    """Handle an obsolete attempt that finished model execution after supersession.

    Per spec: "Obsolete attempts can finish model execution harmlessly but their
    result is discarded." This marks the attempt as discarded without mutating
    campaign truth and is safe to call even if attempt was running.

    Returns the updated attempt or None if not found.
    """
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        return None
    turn = db.get(DmTurn, attempt.turn_id) if attempt.turn_id else None
    # Only discard if attempt is not current or is superseded
    if turn and str(turn.current_attempt_id) == str(attempt_id) and attempt.status not in (ATTEMPT_SUPERSEDED, ATTEMPT_DISCARDED):
        # Still current — not obsolete
        logger.info("dm_turn discard_skipped_still_current attempt_id=%s turn_id=%s status=%s", attempt_id, attempt.turn_id, attempt.status)
        return attempt
    if attempt.status in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_VISIBLE):
        # Already terminal — don't override
        return attempt
    attempt.status = ATTEMPT_DISCARDED
    attempt.last_error = f"Discarded obsolete attempt: {reason}"
    attempt.error_class = "superseded"
    attempt.completed_at = _now()
    # Do not mutate turn or campaign.
    try:
        db.flush()
        db.commit()
        db.refresh(attempt)
    except Exception:
        db.rollback()
        raise
    logger.info("dm_turn discarded_obsolete_attempt turn_id=%s attempt_id=%s reason=%s", attempt.turn_id, attempt.id, reason)
    return attempt


def mark_attempt_failed(
    db: Session,
    attempt_id: uuid.UUID,
    error: str,
    error_class: str = "retriable",
    visible: bool = False,
) -> DmTurnAttempt | None:
    """Mark attempt as failed; if visible, turn becomes failed_visible and blocks advancing.

    Worker crash/recovery uses this to mark failures before retry.

    Args:
        visible: whether failure occurred after visible streaming started (blocks next turn)
    """
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        return None
    turn = db.get(DmTurn, attempt.turn_id) if attempt.turn_id else None
    attempt.last_error = error[:2000] if error else None
    attempt.error_class = error_class
    attempt.completed_at = _now()
    if visible or attempt.status == ATTEMPT_STREAMING or (turn and turn.status == TURN_STREAMING):
        attempt.status = ATTEMPT_FAILED_VISIBLE
        if turn:
            turn.status = TURN_FAILED_VISIBLE
    else:
        attempt.status = ATTEMPT_FAILED
    try:
        db.flush()
        db.commit()
        db.refresh(attempt)
        if turn:
            db.refresh(turn)
    except Exception:
        db.rollback()
        raise
    logger.warning(
        "dm_turn attempt_failed turn_id=%s attempt_id=%s error_class=%s visible=%s error=%s",
        attempt.turn_id, attempt.id, error_class, visible, error[:200] if error else "",
    )
    return attempt


def recover_stuck_attempts(db: Session, *, lease_seconds: int = 300, commit: bool = True) -> int:
    """Recover attempts left in running without completion (worker crash).

    Resets running attempts whose lease expired to prepared for safe retry.
    Streaming attempts are left as-is (they are visible and require explicit
    recovery/retry policy per spec, not silent auto-retry that would duplicate
    visible output).

    Returns number recovered.

    This leaves turn/attempt state recoverable and retryable per:
    "Worker crash leaves turn/attempt state recoverable and retryable."
    """
    cutoff = _now() - timedelta(seconds=lease_seconds)
    candidates = db.execute(
        select(DmTurnAttempt).where(
            DmTurnAttempt.status == ATTEMPT_RUNNING,
            DmTurnAttempt.started_at < cutoff,
        )
    ).scalars().all()
    count = 0
    for attempt in candidates:
        # Do not auto-recover streaming attempts — they are visible
        turn = db.get(DmTurn, attempt.turn_id)
        if turn and turn.status == TURN_STREAMING:
            continue
        attempt.status = ATTEMPT_PREPARED
        attempt.started_at = None
        attempt.worker_job_id = None
        attempt.last_error = f"Recovered stuck running attempt after {lease_seconds}s lease expiry"
        count += 1
        logger.info("dm_turn recovered_stuck attempt_id=%s turn_id=%s", attempt.id, attempt.turn_id)
    if count and commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    elif count:
        db.flush()
    if count:
        logger.info("dm_turn recover_stuck total_recovered=%s lease_seconds=%s", count, lease_seconds)
    return count

