"""Durable DM turn and turn-attempt state machine — issues #200, #206.

One logical DM turn may consume multiple player submissions that arrived
in the same unresolved fictional moment (same campaign+thread/audience).
New eligible submissions invalidate a prepared attempt pre-stream without
mutating campaign truth. Once the first visible chunk commits,
the input set is locked. Stale source-revision attempts are discarded
via optimistic campaign-revision validation. A campaign cannot advance
past an unresolved streaming/failed_visible turn. Staged effects remain
attempt-local through streaming and become authoritative exactly once
on successful narration completion (issue #206).

States
------
DmTurn.status: pending | streaming | succeeded | failed_visible | abandoned
DmTurnAttempt.status: prepared | running | superseded | streaming | succeeded | failed | failed_visible | discarded | abandoned

Three-phase commit (#206): prepared (staged) → streaming/visible (durable chunk) → completed (atomic promotion)

Coordinator
-----------
Decides when unresolved (accepted) submissions for a campaign+thread form a
candidate turn. Called after each accepted submission. Uses a short
SELECT ... FOR UPDATE serialization around assembly/current-attempt
transition (not a long lock across model work) and optimistic revision
check only at commit time.

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

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.campaigns import Campaign
from models.dm import DmTurn
from models.dm import DmTurnAttempt
from models.threads import PlayerSubmission

logger = logging.getLogger(__name__)

# ── Statuses ────────────────────────────────────────────────────────────────

TURN_PENDING = "pending"
TURN_AWAITING_ROLL = "awaiting_roll"
TURN_STREAMING = "streaming"
TURN_SUCCEEDED = "succeeded"
TURN_FAILED_VISIBLE = "failed_visible"
TURN_ABANDONED = "abandoned"

ATTEMPT_PREPARED = "prepared"
ATTEMPT_RUNNING = "running"
ATTEMPT_AWAITING_ROLL = "awaiting_roll"
ATTEMPT_SUPERSEDED = "superseded"
ATTEMPT_STREAMING = "streaming"
ATTEMPT_SUCCEEDED = "succeeded"
ATTEMPT_FAILED = "failed"
ATTEMPT_FAILED_VISIBLE = "failed_visible"
ATTEMPT_DISCARDED = "discarded"
ATTEMPT_ABANDONED = "abandoned"

ACTIVE_TURN_STATUSES = {TURN_PENDING, TURN_AWAITING_ROLL, TURN_STREAMING, TURN_FAILED_VISIBLE}
BLOCKING_TURN_STATUSES = {TURN_AWAITING_ROLL, TURN_STREAMING, TURN_FAILED_VISIBLE}
PRE_STREAM_ATTEMPT_STATUSES = {ATTEMPT_PREPARED, ATTEMPT_RUNNING}
VISIBLE_ATTEMPT_STATUSES = {ATTEMPT_STREAMING, ATTEMPT_FAILED_VISIBLE}
ABANDONED_STATUSES = {ATTEMPT_ABANDONED, TURN_ABANDONED}


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
    """All accepted submissions for this campaign+thread, ordered by sequence."""
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
    """Most recent turn in blocking/pending state for this campaign+thread."""
    tid = str(thread_id)
    turn = db.execute(
        select(DmTurn)
        .where(DmTurn.campaign_id == campaign_id, DmTurn.thread_id == tid, DmTurn.status.in_(list(ACTIVE_TURN_STATUSES)))
        .order_by(DmTurn.created_at.desc())
        .limit(1)
    ).scalars().first()
    return turn


def _get_active_turn_for_update(db: Session, campaign_id: uuid.UUID, thread_id: str) -> DmTurn | None:
    """Lock-aware fetch of active turn for CAS serialization.

    Uses FOR UPDATE where the dialect supports it; SQLite in tests silently
    ignores it which is acceptable for single-threaded test execution.
    The partial unique index on (campaign_id, thread_id) where active
    provides the concurrent-insert safety net even without row locking.
    """
    tid = str(thread_id)
    # Try FOR UPDATE, fall back to plain read if dialect does not support it
    try:
        return db.execute(
            select(DmTurn)
            .where(DmTurn.campaign_id == campaign_id, DmTurn.thread_id == tid, DmTurn.status.in_(list(ACTIVE_TURN_STATUSES)))
            .order_by(DmTurn.created_at.desc())
            .limit(1)
            .with_for_update()
        ).scalars().first()
    except Exception:
        # SQLite or any dialect that rejects FOR UPDATE in this context
        return get_active_turn(db, campaign_id, thread_id)


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
    """Whether any streaming/failed_visible turn blocks advancing."""
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
    *,
    commit: bool = True,
) -> tuple[DmTurn, DmTurnAttempt] | None:
    """Decide when unresolved submissions form a candidate turn.

    Transaction ownership is at the caller's boundary. By default ``commit=True``
    for standalone/test usage (commits internally). When called from inside
    ``execute_http_idempotent()``'s callback (e.g. submission acceptance), pass
    ``commit=False`` so the outer idempotency layer commits the submission +
    turn + ``IdempotentCommand`` atomically (flush-only here).

    Uses a short FOR UPDATE serialization around active-turn read and a
    partial unique index on active turns to prevent concurrent inserts from
    creating competing pending turns (CAS, not a long lock across model work).

    Returns (turn, current_attempt) if a turn is active/created, None if no
    unresolved submissions.

    Raises:
        TurnConflictError: if a streaming/failed_visible turn blocks new turns.
        StreamBoundaryError: if new submissions would alter a post-stream input set.
    """
    start_wait = time.monotonic()
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found")
    tid = str(thread_id)

    # Issue #243 — fail closed on the lobby OOC thread. Lobby chat is
    # non-fictional table talk and must never assemble a forward DM turn.
    # The lobby chat endpoint never calls the coordinator and the gameplay
    # submission endpoint refuses lobby threads; this guard covers any other
    # (present or future) caller that resolves a lobby thread id.
    try:
        from models.threads import CampaignThread as _CampaignThread

        _tid_uuid = uuid.UUID(tid)
    except (ValueError, AttributeError, TypeError):
        _tid_uuid = None
    if _tid_uuid is not None:
        _thread = db.get(_CampaignThread, _tid_uuid)
        if _thread is not None and _thread.thread_type == "lobby":
            logger.warning(
                "dm_turn coordination refused campaign_id=%s thread_id=%s reason=lobby_thread",
                campaign_id,
                tid,
            )
            return None

    # Short serialization: acquire stable campaign row lock BEFORE collecting
    # authoritative unresolved set, so a concurrent committer's submissions
    # are visible after we acquire the lock (Postgres READ COMMITTED).
    # This prevents dropping A when B collected {B} before waiting on lock.
    try:
        db.execute(select(Campaign).where(Campaign.id == campaign_id).with_for_update())
        # Re-read campaign to see revision after any waiter
        db.refresh(campaign)
    except Exception:
        pass
    source_revision = int(campaign.revision) if campaign.revision is not None else 0

    unresolved = _collect_unresolved_submissions(db, campaign_id, tid)
    if not unresolved:
        logger.info(
            "dm_turn coordinator no_work campaign_id=%s thread_id=%s source_revision=%s",
            campaign_id, tid, source_revision,
        )
        return None

    active = _get_active_turn_for_update(db, campaign_id, tid)

    # No active turn → create new logical turn from all unresolved submissions.
    if active is None:
        sub_ids = [str(s.id) for s in unresolved]
        window_start = min(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
        window_end = max(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
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
        # Isolate candidate insert in savepoint so IntegrityError does not roll back caller's outer txn
        # (e.g. submission + IdempotentCommand when commit=False)
        if db.new or db.dirty or db.deleted:
            try:
                db.flush()
            except Exception:
                pass
        _insert_succeeded = False
        try:
            with db.begin_nested():
                db.add(turn)
                db.flush()
            _insert_succeeded = True
        except IntegrityError:
            # Concurrent inserter won the unique-active race; re-read winner
            # (savepoint rolled back automatically; outer txn intact)
            try:
                db.expunge(turn)
            except Exception:
                pass
            active_retry = _get_active_turn_for_update(db, campaign_id, tid)
            if active_retry is not None:
                unresolved_retry = _collect_unresolved_submissions(db, campaign_id, tid)
                active = active_retry
                unresolved = unresolved_retry
            else:
                raise

        # If we did not hit the IntegrityError path, create attempt
        if active is None and _insert_succeeded:
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
            waiting_ms = int((time.monotonic() - start_wait) * 1000)
            turn.time_waiting_ms = waiting_ms
            db.flush()
            if commit:
                db.commit()
                db.refresh(turn)
                db.refresh(attempt)
            else:
                # Keep in session for outer commit; still refresh from flush state where possible
                try:
                    db.flush()
                except Exception:
                    pass
            logger.info(
                "dm_turn created campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s source_revision=%s input_set_revision=1 "
                "submission_count=%s submission_ids=%s assembly_window_start=%s assembly_window_end=%s time_waiting_ms=%s",
                campaign_id, tid, turn.id, attempt.id, source_revision, len(sub_ids), sub_ids,
                window_start.isoformat(), window_end.isoformat(), waiting_ms,
            )
            return turn, attempt
        # else we had a concurrent winner; fall through to pending handling below
        # (active now points to winner, unresolved refreshed)

    # Active turn exists. If it is streaming/failed_visible, it blocks expansion.
    if active.status in BLOCKING_TURN_STATUSES:
        active_ids = set(active.submission_ids or [])
        new_ids = [str(s.id) for s in unresolved]
        if set(new_ids) == active_ids:
            cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
            if cur is None:
                return active, None  # type: ignore[return-value]
            return active, cur
        cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
        if cur and cur.status in (ATTEMPT_STREAMING, ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_VISIBLE):
            logger.warning(
                "dm_turn stream_boundary_blocked campaign_id=%s thread_id=%s blocking_turn_id=%s attempt_id=%s "
                "existing_submissions=%s new_submissions=%s source_revision=%s",
                campaign_id, tid, active.id, cur.id, sorted(active_ids), sorted(set(new_ids)), source_revision,
            )
            raise StreamBoundaryError(active.id, cur.id)
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
        cur = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
        logger.info(
            "dm_turn coordinator no_change campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s submission_count=%s",
            campaign_id, tid, active.id, cur.id if cur else None, len(new_ids_ordered),
        )
        return active, cur  # type: ignore[return-value]

    # Input set expanded pre-stream — must supersede old attempt, create new one.
    # Lock current attempt for CAS
    cur_attempt = None
    if active.current_attempt_id:
        try:
            cur_attempt = db.execute(
                select(DmTurnAttempt).where(DmTurnAttempt.id == active.current_attempt_id).with_for_update()
            ).scalars().first()
        except Exception:
            cur_attempt = db.get(DmTurnAttempt, active.current_attempt_id)
    if cur_attempt is None:
        logger.warning("dm_turn pending_without_attempt campaign_id=%s thread_id=%s turn_id=%s", campaign_id, tid, active.id)
        new_rev = active.input_set_revision + 1
        window_start = active.assembly_window_start or _now()
        window_end = max(s.accepted_at for s in unresolved if s.accepted_at) if unresolved[0].accepted_at else _now()
        if window_end and window_end.tzinfo is None:
            window_end = window_end.replace(tzinfo=timezone.utc)
        new_attempt = DmTurnAttempt(
            id=uuid.uuid4(),
            turn_id=active.id,
            attempt_number=1,
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
        active.source_revision = source_revision
        db.flush()
        if commit:
            db.commit()
            db.refresh(active)
            db.refresh(new_attempt)
        logger.info(
            "dm_turn superseded_no_prior_attempt campaign_id=%s thread_id=%s turn_id=%s new_attempt_id=%s "
            "input_set_revision=%s submission_count=%s submission_ids=%s source_revision=%s invalidation_reason=new_eligible_submission_pre_stream",
            campaign_id, tid, active.id, new_attempt.id, new_rev, len(new_ids_ordered), new_ids_ordered, source_revision,
        )
        return active, new_attempt

    if cur_attempt.status == ATTEMPT_STREAMING or active.streaming_started_at is not None:
        logger.warning(
            "dm_turn stream_boundary_blocked campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s status=%s input_set_revision=%s",
            campaign_id, tid, active.id, cur_attempt.id, cur_attempt.status, active.input_set_revision,
        )
        raise StreamBoundaryError(active.id, cur_attempt.id)

    if cur_attempt.status not in PRE_STREAM_ATTEMPT_STATUSES:
        if cur_attempt.status in (ATTEMPT_FAILED, ATTEMPT_DISCARDED):
            pass
        else:
            logger.warning(
                "dm_turn supersession_blocked_status campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s status=%s",
                campaign_id, tid, active.id, cur_attempt.id, cur_attempt.status,
            )
            raise StreamBoundaryError(active.id, cur_attempt.id)

    # Safe to supersede pre-stream attempt — CAS: ensure we still own current_attempt_id
    # Use conditional update to prevent concurrent supersession from creating duplicate lineage
    # We already hold FOR UPDATE on cur_attempt and active, so this is serialized.
    old_attempt_id = cur_attempt.id
    old_status = cur_attempt.status
    # Verify still current after lock
    db.refresh(active)
    if str(active.current_attempt_id) != str(old_attempt_id):
        # Lost race: another transaction superseded first
        # Re-read new current and retry as no_change or supersede again
        logger.info("dm_turn supersede_race_lost campaign_id=%s thread_id=%s turn_id=%s expected_current=%s actual_current=%s",
                    campaign_id, tid, active.id, old_attempt_id, active.current_attempt_id)
        # Re-collect to see if we still need expansion
        cur_retry = db.get(DmTurnAttempt, active.current_attempt_id) if active.current_attempt_id else None
        if cur_retry and set(cur_retry.submission_ids or []) == new_ids_set:
            return active, cur_retry
        # Otherwise treat as concurrent supersession succeeded; caller can retry outer coordination
        raise StreamBoundaryError(active.id, old_attempt_id)

    cur_attempt.status = ATTEMPT_SUPERSEDED
    cur_attempt.invalidation_reason = "new_eligible_submission_pre_stream"
    cur_attempt.invalidated_at = _now()

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

    active.submission_ids = list(new_ids_ordered)
    active.input_set_revision = new_rev
    active.current_attempt_id = new_attempt.id
    active.assembly_window_end = window_end
    db.flush()
    waiting_ms = int((time.monotonic() - start_wait) * 1000)
    if commit:
        db.commit()
        db.refresh(active)
        db.refresh(cur_attempt)
        db.refresh(new_attempt)
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


# ── Staged effects — issue #206 ─────────────────────────────────────────────

def stage_validated_attempt(
    db: Session,
    attempt_id: uuid.UUID,
    contract: Any,
) -> DmTurnAttempt:
    """Persist typed staged effects attach to one attempt without mutating authoritative state.

    Must be called after validator pipeline passes and before streaming. Crash during
    staging leaves authoritative state untouched. Staged effects remain attempt-local
    through streaming and are promoted exactly once on successful commit.

    Args:
        db: Session
        attempt_id: DmTurnAttempt id (must be prepared/running)
        contract: DmTurnContractV1 instance or dict with staged_effects

    Returns:
        Updated DmTurnAttempt with staged_effects + contract_snapshot persisted.
    """
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        raise ValueError(f"Attempt {attempt_id} not found")
    if attempt.status not in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
        raise ValueError(f"Attempt {attempt_id} cannot stage effects from status {attempt.status}")

    # Normalize contract to dict + list
    if hasattr(contract, "model_dump"):
        contract_dict = contract.model_dump(mode="json")
        staged = getattr(contract, "staged_effects", []) or []
        if hasattr(staged, "__iter__") and staged and hasattr(staged[0], "model_dump"):
            staged_list = [e.model_dump(mode="json") for e in staged]
        else:
            staged_list = list(staged)
    elif isinstance(contract, dict):
        contract_dict = contract
        staged_list = contract.get("staged_effects") or []
    else:
        raise ValueError("contract must be DmTurnContractV1 or dict")

    # Validate staged_effects against contract invariants (no generic SQLalready validated by contract)
    # Ensure staged_effects only in respond mode is enforced by contract; here we just persist
    # Effect-ID uniqueness is re-checked here (not just in contract validation)
    # because callers may stage from a raw dict: downstream idempotency keys
    # derive from attempt.id + effect.id, so duplicates would silently drop
    # later same-type writes at commit.
    seen_ids: set[str] = set()
    seen_keys: set[str] = set()
    for eff in staged_list:
        if isinstance(eff, dict):
            eid, args = eff.get("id"), eff.get("arguments") or {}
        else:
            eid, args = getattr(eff, "id", None), getattr(eff, "arguments", None) or {}
        if not isinstance(args, dict):
            args = args.model_dump(mode="json") if hasattr(args, "model_dump") else {}
        eid = str(eid) if eid is not None else ""
        if not eid or eid in seen_ids:
            raise ValueError("staged_effect ids must be unique")
        seen_ids.add(eid)
        xkey = str((args or {}).get("idempotency_key") or "").strip()
        if xkey:
            if xkey in seen_keys:
                raise ValueError("staged_effect explicit idempotency keys must be unique")
            seen_keys.add(xkey)
    attempt.staged_effects = staged_list
    attempt.contract_snapshot = contract_dict
    # Idempotency key defaults to attempt.id for duplicate commit detection
    if not attempt.commit_operation_id:
        attempt.commit_operation_id = str(attempt.id)

    db.flush()
    try:
        db.commit()
        db.refresh(attempt)
    except Exception:
        db.rollback()
        raise

    logger.info(
        "dm_turn staged_effects_persisted turn_id=%s attempt_id=%s count=%s types=%s commit_operation_id=%s",
        attempt.turn_id, attempt.id, len(staged_list), [e.get("effect_type") for e in (staged_list or [])], attempt.commit_operation_id,
    )
    return attempt


# ── Stream-start commitment boundary ────────────────────────────────────────


def mark_streaming_started(db: Session, turn_id: uuid.UUID, attempt_id: uuid.UUID, stream_id: uuid.UUID | str | None = None, *, commit: bool = True) -> tuple[DmTurn, DmTurnAttempt]:
    """Establish stream-start commitment boundary for the input set.

    After this call, the turn's input set is locked; any new eligible submissions
    will be rejected with StreamBoundaryError until the turn resolves, and a new
    turn cannot be assembled. This implements: "Once the first visible chunk commits,
    the turn input set cannot silently change."

    When stream_id is provided, verifies first durable chunk is persisted before
    promotion — this enforces the visible-commitment boundary without promoting
    staged gameplay effects. Staged effects remain attempt-local until atomic commit.

    When ``commit`` is False the transition is only flushed (no commit), so a
    caller can atomically commit it together with the first chunk row in a
    single transaction (crash-atomic #206 boundary). The caller owns the
    commit; on CAS failure the current transaction is rolled back.

    Idempotent: if already streaming with same attempt, returns without error.
    Uses FOR UPDATE + CAS to ensure only the current attempt can become streaming.
    """
    # Lock both rows for CAS
    try:
        turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
        attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
    except Exception:
        turn = db.get(DmTurn, turn_id)
        attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")

    # Issue #265 — first-visible boundary participates in lifecycle
    # serialization: lock the campaign row (the same lock archive/restore
    # holds) and refuse dormancy crossings. Chunk 0 and this decision share
    # one commit, so archive can neither slip between check and persist nor
    # strand visible output on an archived table. Lock order is turn/attempt
    # rows first, campaign row second — archive only takes the campaign row.
    from app.campaigns.service import require_playable_campaign

    try:
        from models.campaigns import Campaign as _Campaign

        _locked_campaign = db.execute(
            select(_Campaign).where(_Campaign.id == turn.campaign_id).with_for_update()
            .execution_options(populate_existing=True)
        ).scalars().first()
    except Exception:
        from models.campaigns import Campaign as _CampaignFallback

        _locked_campaign = db.get(_CampaignFallback, turn.campaign_id)
    require_playable_campaign(_locked_campaign)

    from app.rolls.service import has_pending_rolls
    if has_pending_rolls(db, turn.id):
        raise ValueError(f"Turn {turn_id} has pending player-owned rolls and cannot stream outcome narration")

    # ── Durable chunk boundary (issue #206) — fail closed if missing ───────
    if stream_id is None or str(stream_id).strip() == "":
        raise ValueError("stream_id is required — transition to streaming requires durable first chunk")

    if turn.status == TURN_STREAMING and str(turn.streaming_attempt_id) == str(attempt_id) and attempt.status == ATTEMPT_STREAMING:
        # Idempotent — ensure supplied stream matches persisted
        if attempt.stream_id is not None and str(attempt.stream_id) != str(stream_id):
            raise ValueError(
                f"Attempt {attempt_id} already streaming with stream {attempt.stream_id}, cannot switch to {stream_id}"
            )
        return turn, attempt

    # ── Durable chunk boundary (issue #206) ────────────────────────────────
    parsed_stream_id: uuid.UUID | None = None
    try:
        parsed_stream_id = uuid.UUID(str(stream_id))
    except ValueError as exc:
        raise ValueError(f"Invalid stream_id {stream_id}") from exc
    # Verify stream exists and has at least one durable chunk
    from models.dm import DMStream
    from models.dm import DMStreamChunk
    stream = db.get(DMStream, parsed_stream_id)
    if stream is None:
        raise ValueError(f"Stream {parsed_stream_id} not found")
    # Check that at least one chunk exists (first visible chunk durably persisted)
    has_chunk = db.execute(
        select(DMStreamChunk).where(DMStreamChunk.stream_id == parsed_stream_id).limit(1)
    ).scalars().first()
    if has_chunk is None:
        # Also check denormalized counters for legacy
        if not stream.first_chunk_at and (stream.chunk_count or 0) == 0:
            raise ValueError(f"Streaming requires durable first chunk for stream {parsed_stream_id} — no chunk persisted")
    # Ensure stream belongs to this turn/attempt — fail closed (prevents unrelated chunk satisfying boundary)
    if str(stream.turn_id) != str(turn_id) or str(stream.attempt_id) != str(attempt_id):
        raise ValueError(
            f"Stream {parsed_stream_id} does not belong to turn {turn_id} attempt {attempt_id} "
            f"(stream.turn_id={stream.turn_id} stream.attempt_id={stream.attempt_id})"
        )

    if attempt.status == ATTEMPT_SUPERSEDED:
        raise AttemptSupersededError(attempt_id, attempt.invalidation_reason)
    if attempt.status not in (ATTEMPT_PREPARED, ATTEMPT_RUNNING):
        raise ValueError(f"Attempt {attempt_id} cannot transition to streaming from status {attempt.status}")
    if turn.status not in (TURN_PENDING,):
        raise ValueError(f"Turn {turn_id} cannot transition to streaming from status {turn.status}")
    if str(turn.current_attempt_id) != str(attempt_id):
        raise AttemptSupersededError(attempt_id, "superseded_by_newer_attempt_pre_stream")

    # CAS: atomically verify still current and pending via conditional update
    now = _now()
    # Use update with WHERE to ensure we haven't been superseded between read and write
    result = db.execute(
        update(DmTurn)
        .where(DmTurn.id == turn_id, DmTurn.status == TURN_PENDING, DmTurn.current_attempt_id == attempt_id)
        .values(status=TURN_STREAMING, streaming_started_at=now, streaming_attempt_id=attempt_id, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        db.rollback()
        # Re-read to determine cause
        fresh_turn = db.get(DmTurn, turn_id)
        fresh_attempt = db.get(DmTurnAttempt, attempt_id)
        if fresh_attempt and fresh_attempt.status == ATTEMPT_SUPERSEDED:
            raise AttemptSupersededError(attempt_id, fresh_attempt.invalidation_reason)
        if fresh_turn and str(fresh_turn.current_attempt_id) != str(attempt_id):
            raise AttemptSupersededError(attempt_id, "superseded_by_newer_attempt_pre_stream")
        raise ValueError(f"Turn {turn_id} CAS failed for streaming transition (status={fresh_turn.status if fresh_turn else 'unknown'})")

    attempt_values: dict[str, Any] = dict(status=ATTEMPT_STREAMING, streaming_started_at=now, started_at=attempt.started_at or now, updated_at=now)
    if parsed_stream_id is not None:
        attempt_values["stream_id"] = parsed_stream_id
    result2 = db.execute(
        update(DmTurnAttempt)
        .where(DmTurnAttempt.id == attempt_id, DmTurnAttempt.status.in_([ATTEMPT_PREPARED, ATTEMPT_RUNNING]))
        .values(**attempt_values)
        .execution_options(synchronize_session=False)
    )
    if result2.rowcount == 0:
        db.rollback()
        raise ValueError(f"Attempt {attempt_id} CAS failed for streaming transition")

    if commit:
        db.commit()
        db.refresh(turn)
        db.refresh(attempt)
    else:
        # Flush-only: caller atomically commits this together with the first
        # chunk row (single commit point). Refresh is deferred until after
        # the caller's commit; in-memory state is set explicitly below.
        db.flush()
    # Ensure in-memory reflects streaming
    turn.status = TURN_STREAMING
    turn.streaming_started_at = now
    turn.streaming_attempt_id = attempt_id
    attempt.status = ATTEMPT_STREAMING
    attempt.streaming_started_at = now
    if parsed_stream_id is not None:
        attempt.stream_id = parsed_stream_id

    logger.info(
        "dm_turn streaming_started campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s source_revision=%s input_set_revision=%s submission_ids=%s stream_id=%s staged_effect_count=%s",
        turn.campaign_id, turn.thread_id, turn.id, attempt.id, attempt.source_revision, attempt.input_set_revision, attempt.submission_ids,
        str(parsed_stream_id) if parsed_stream_id else None,
        len(attempt.staged_effects or []),
    )
    return turn, attempt


def mark_recovered_streaming(
    db: Session, turn_id: uuid.UUID, attempt_id: uuid.UUID, *, commit: bool = True
) -> tuple[DmTurn, DmTurnAttempt]:
    """Repair a failed-visible turn/attempt back to streaming after recovery.

    Only for the partial-stream recovery path: the attempt must be the
    current failed-visible attempt with a completed stream and a preserved
    valid ``contract_snapshot``. New input can never enter through here
    (input set stays locked); the only exit is the normal
    ``commit_turn_with_effects``. Raises ``ValueError`` otherwise.
    """
    try:
        turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
        attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
    except Exception:
        turn = db.get(DmTurn, turn_id)
        attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")
    if str(turn.current_attempt_id) != str(attempt_id):
        raise ValueError(f"Attempt {attempt_id} is not current for turn {turn_id}")
    if attempt.status != ATTEMPT_FAILED_VISIBLE or turn.status != TURN_FAILED_VISIBLE:
        raise ValueError(
            f"Attempt {attempt_id} status {attempt.status} / turn {turn_id} status {turn.status} "
            "cannot recover; must be failed_visible"
        )
    if not attempt.contract_snapshot:
        raise ValueError(f"Attempt {attempt_id} has no preserved structured result to recover")
    if attempt.stream_id is None:
        raise ValueError(f"Attempt {attempt_id} has no stream to recover")
    from models.dm import DMStream

    stream = db.get(DMStream, attempt.stream_id)
    if stream is None or stream.status != "completed":
        raise ValueError(f"Attempt {attempt_id} stream is not completed; cannot recover")
    now = _now()
    attempt.status = ATTEMPT_STREAMING
    attempt.last_error = None
    attempt.error_class = None
    attempt.completed_at = None
    turn.status = TURN_STREAMING
    if turn.streaming_attempt_id is None:
        turn.streaming_attempt_id = attempt_id
    if attempt.streaming_started_at is None:
        attempt.streaming_started_at = now
    if turn.streaming_started_at is None:
        turn.streaming_started_at = now
    db.add(attempt)
    db.add(turn)
    if commit:
        db.commit()
        db.refresh(turn)
        db.refresh(attempt)
    else:
        db.flush()
    logger.info(
        "dm_turn recovered_streaming turn_id=%s attempt_id=%s stream_id=%s",
        turn.id, attempt.id, attempt.stream_id,
    )
    return turn, attempt


def mark_attempt_running(db: Session, attempt_id: uuid.UUID, worker_job_id: uuid.UUID | None = None) -> DmTurnAttempt:
    """Mark attempt as running (worker claimed). Recoverable if worker crashes."""
    now = _now()
    values = {"status": ATTEMPT_RUNNING, "started_at": now}
    if worker_job_id:
        values["worker_job_id"] = worker_job_id
    claimed = db.execute(
        update(DmTurnAttempt)
        .where(DmTurnAttempt.id == attempt_id, DmTurnAttempt.status == ATTEMPT_PREPARED)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:
        db.rollback()
        raise ValueError(f"Attempt {attempt_id} is missing or is no longer prepared")
    db.commit()
    attempt = db.get(DmTurnAttempt, attempt_id)
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
    commit: bool = True,
) -> tuple[DmTurn, DmTurnAttempt, Any]:
    """Commit a DM turn's authoritative effects with optimistic revision validation.

    Only the committed streaming current attempt can be committed (CAS).
    Stale source-revision attempts are rejected without mutating campaign truth.

    With ``commit=False`` the commit is flush-only so a caller (e.g. partial-
    stream recovery inside an idempotent command) can atomically commit it
    together with preceding recovery writes in a single transaction.
    """
    from app.campaigns.events import RevisionConflictError, commit_campaign_mutation

    # Lock for CAS
    try:
        turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
        attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
    except Exception:
        turn = db.get(DmTurn, turn_id)
        attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")

    from app.rolls.service import has_pending_rolls
    if has_pending_rolls(db, turn.id):
        raise ValueError(f"Turn {turn_id} has pending player-owned rolls and cannot commit an outcome")

    # Obsolete attempt check — must be current streaming attempt
    if str(turn.current_attempt_id) != str(attempt_id):
        logger.info(
            "dm_turn commit_discarded_superseded campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s current_attempt_id=%s status=%s invalidation_reason=%s",
            turn.campaign_id, turn.thread_id, turn.id, attempt.id, turn.current_attempt_id, attempt.status, attempt.invalidation_reason,
        )
        # Mark as discarded if not already terminal
        if attempt.status not in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_VISIBLE, ATTEMPT_SUPERSEDED):
            attempt.status = ATTEMPT_DISCARDED
            attempt.last_error = f"Discarded superseded attempt; current attempt is {turn.current_attempt_id}"
            attempt.error_class = "superseded"
            try:
                db.flush()
                db.commit()
            except Exception:
                db.rollback()
        raise AttemptSupersededError(attempt_id, attempt.invalidation_reason)

    if attempt.status == ATTEMPT_SUPERSEDED:
        raise AttemptSupersededError(attempt_id, attempt.invalidation_reason)

    # ── Idempotency: duplicate commit short-circuit (issue #206) ─────────────
    duplicate_op = operation_id or str(attempt.id)
    # If attempt already succeeded, return existing result without duplicate mutation (idempotent retry)
    if attempt.status == ATTEMPT_SUCCEEDED and turn.status == TURN_SUCCEEDED:
        from models.campaigns import CampaignDomainEvent

        existing = None
        if attempt.result:
            try:
                rid = (attempt.result or {}).get("event_id") or (attempt.result or {}).get("id")
                if rid:
                    existing = db.get(CampaignDomainEvent, uuid.UUID(str(rid)))
            except Exception:
                existing = None
        if existing is None:
            existing = db.execute(
                select(CampaignDomainEvent).where(CampaignDomainEvent.campaign_id == turn.campaign_id, CampaignDomainEvent.operation_id == duplicate_op)
            ).scalars().first()
        if existing is not None:
            logger.info(
                "dm_turn duplicate_commit_hit campaign_id=%s turn_id=%s attempt_id=%s operation_id=%s event_id=%s",
                turn.campaign_id, turn.id, attempt.id, duplicate_op, existing.id,
            )
            return turn, attempt, existing
        # Fallback: already succeeded but no event found — treat as duplicate (should not happen)
        if attempt.result:
            logger.info("dm_turn duplicate_commit_hit_no_event turn_id=%s attempt_id=%s", turn.id, attempt.id)
            # Return with stored result as pseudo-event
            return turn, attempt, attempt.result

    # Also check for duplicate before any mutation even if attempt not yet marked succeeded (crash-after-commit replay)
    if duplicate_op:
        from models.campaigns import CampaignDomainEvent

        dup_event = db.execute(
            select(CampaignDomainEvent).where(CampaignDomainEvent.campaign_id == turn.campaign_id, CampaignDomainEvent.operation_id == duplicate_op)
        ).scalars().first()
        if dup_event is not None and attempt.status != ATTEMPT_SUCCEEDED:
            logger.info(
                "dm_turn duplicate_commit_hit_pre_mark campaign_id=%s turn_id=%s attempt_id=%s operation_id=%s event_id=%s",
                turn.campaign_id, turn.id, attempt.id, duplicate_op, dup_event.id,
            )
            now_dup = _now()
            if attempt.status == ATTEMPT_STREAMING:
                attempt.status = ATTEMPT_SUCCEEDED
                attempt.completed_at = now_dup
                attempt.result = dup_event.to_dict() if hasattr(dup_event, "to_dict") else {"event_id": str(dup_event.id)}
            if turn.status == TURN_STREAMING:
                turn.status = TURN_SUCCEEDED
                turn.resolved_at = now_dup
                turn.committed_at = now_dup
            try:
                db.flush()
                if commit:
                    db.commit()
                db.refresh(turn)
                db.refresh(attempt)
            except Exception:
                db.rollback()
            return turn, attempt, dup_event

    # Only streaming attempt can commit authoritative effects — commitment boundary (after idempotency check)
    if attempt.status != ATTEMPT_STREAMING or turn.status != TURN_STREAMING:
        raise ValueError(f"Attempt {attempt_id} status {attempt.status} / turn {turn_id} status {turn.status} cannot commit; must be streaming (stream-start boundary)")
    if str(turn.streaming_attempt_id) != str(attempt_id):
        raise ValueError(f"Attempt {attempt_id} is not the streaming attempt for turn {turn_id}")

    # Ensure turn is not blocked by another visible partial turn
    blocking = _has_blocking_turn(db, turn.campaign_id, turn.thread_id, exclude_turn_id=turn.id)
    if blocking is not None:
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
        logger.warning(
            "dm_turn stale_source_revision_conflict campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s expected=%s actual=%s source_revision=%s",
            turn.campaign_id, turn.thread_id, turn.id, attempt.id, expected, actual, attempt.source_revision,
        )
        attempt.last_error = f"Stale source_revision: expected {expected}, actual {actual}"
        attempt.error_class = "stale_revision_visible"
        attempt.completed_at = _now()
        attempt.status = ATTEMPT_FAILED_VISIBLE
        turn.status = TURN_FAILED_VISIBLE
        try:
            db.flush()
            if commit:
                db.commit()
        except Exception:
            db.rollback()
        raise StaleRevisionError(turn.campaign_id, int(expected), actual, attempt.id)

    execute_start = time.monotonic()
    # Issue #248 — private-turn audience scope. A canonical turn on a
    # private thread commits its domain event with restricted visibility so
    # no shared feed/projection broadcasts the private action, while the
    # thread-scoped payload preserves causal links for later DM reasoning,
    # post-turn processing, and repair (context assembly scopes
    # RECENT_HISTORY records by payload/provenance thread_id).
    attempt_audience = str(
        getattr(attempt, "audience", None) or getattr(turn, "audience", None) or "campaign"
    )
    is_private_turn = attempt_audience == "private"
    turn_visibility = "private" if is_private_turn else "public"
    turn_provenance: dict[str, Any] = {
        "source": "dm_turn",
        "thread_id": str(turn.thread_id),
        "audience": attempt_audience,
        "attempt_id": str(attempt.id),
    }
    # Build enriched payload that includes staged effects metadata for audit
    base_payload = payload or {"turn_id": str(turn.id), "attempt_id": str(attempt.id), "submission_ids": attempt.submission_ids or []}
    base_payload = dict(base_payload)
    base_payload.setdefault("thread_id", str(turn.thread_id))
    base_payload.setdefault("audience", attempt_audience)
    # Include staged effect ids/types in payload for observability
    staged_list = attempt.staged_effects or []
    adventure_completion_args: dict | None = None
    if staged_list:
        base_payload = dict(base_payload)
        base_payload["staged_effect_ids"] = [e.get("id") for e in staged_list]
        base_payload["staged_effect_types"] = [e.get("effect_type") for e in staged_list]
        if attempt.stream_id:
            base_payload["stream_id"] = str(attempt.stream_id)
        # A staged adventure completion promotes the turn commit to the
        # adventure.completed domain event (issue #260): the turn IS the
        # authoritative provenance for the DM's completion decision. Only
        # the default turn event type is promoted — explicit callers keep
        # their event type.
        adventure_completion_args = next(
            (e.get("arguments") or {} for e in staged_list if e.get("effect_type") == "complete_adventure"),
            None,
        )
        if adventure_completion_args is not None and event_type == "dm.turn_resolved":
            event_type = "adventure.completed"
            # Player-readable lifecycle data only — the DM's completion
            # reason stays on the owner-visible adventure row, never in the
            # public domain-event feed (issue #260 security).
            base_payload["adventure_completion"] = {
                "outcome": adventure_completion_args.get("outcome"),
                "public_summary": adventure_completion_args.get("public_summary"),
                "adventure_id": adventure_completion_args.get("adventure_id"),
            }
            base_payload["outcome"] = adventure_completion_args.get("outcome")
            base_payload["public_summary"] = adventure_completion_args.get("public_summary")
            base_payload["source_turn_id"] = str(turn.id)

    # Wrap mutate to also apply staged effects atomically inside same revision bump
    # Resolved adventure identity closed by this turn (issue #260): populated
    # inside the mutation, consumed by the post-mutate payload builder so the
    # authoritative completion event carries the actual adventure id even when
    # the effect targeted the implicit current adventure.
    resolved_adventure: dict[str, str] = {}

    # JIT identity decision records collected inside the locked revision
    # transaction and flushed only after commit (issue #214): persisting
    # decision_telemetry on an independent session while the campaign row
    # is FOR UPDATE-locked would stall on the parent FK lock.
    identity_telemetry_outbox: list = []

    def _mutate_with_effects(campaign):
        # Apply caller-provided mutate first
        if mutate is not None:
            mutate(campaign)
        # Apply staged effects via registry (fail-closed)
        if staged_list:
            from app.dm.effects import apply_staged_effects

            apply_staged_effects(db, campaign, staged_list, turn, attempt)
        if adventure_completion_args is not None:
            try:
                from models.campaigns import Adventure as _Adventure

                _closed = (
                    db.execute(
                        select(_Adventure).where(
                            _Adventure.campaign_id == turn.campaign_id,
                            _Adventure.status == "completed",
                            _Adventure.source_turn_id == turn.id,
                        )
                    )
                    .scalars()
                    .first()
                )
                if _closed is not None:
                    resolved_adventure["adventure_id"] = str(_closed.id)
                    resolved_adventure["title"] = _closed.title or ""
            except Exception as e:
                logger.warning("dm_turn failed to resolve completed adventure turn_id=%s error=%s", turn.id, e)
        # JIT-promote committed new-entity proposals to durable canonical
        # identity exactly once (issue #209). Runs in the same revision
        # transaction: failed commit leaves no half-created authority.
        # Idempotency key per (attempt, temp_id) makes retries safe.
        try:
            from app.world.service import promote_new_entities_from_contract

            promoted = promote_new_entities_from_contract(
                db, campaign, turn, attempt,
                identity_telemetry_outbox=identity_telemetry_outbox)
            if promoted:
                base_payload["promoted_entity_ids"] = [str(e.id) for e in promoted]
                base_payload["promoted_entity_types"] = [e.entity_type for e in promoted]
        except ImportError:
            pass

    def _adventure_event_payload() -> dict:
        """Post-mutate payload: same lifecycle fields, resolved adventure id."""
        if adventure_completion_args is not None and resolved_adventure.get("adventure_id"):
            merged = dict(base_payload)
            merged["adventure_completion"] = {
                **merged.get("adventure_completion", {}),
                "adventure_id": resolved_adventure["adventure_id"],
                "title": resolved_adventure.get("title"),
            }
            return merged
        return base_payload

    # Persist commit_operation_id for idempotency
    if not attempt.commit_operation_id:
        attempt.commit_operation_id = duplicate_op
        db.flush()

    try:
        campaign_after, event = commit_campaign_mutation(
            db,
            turn.campaign_id,
            expected_revision=int(expected),
            event_type=event_type,
            payload=None if event_type == "adventure.completed" else base_payload,
            operation_id=duplicate_op,
            actor_id=actor_id,
            visibility=turn_visibility,
            provenance=turn_provenance,
            mutate=_mutate_with_effects,
            commit=False,
            payload_builder=_adventure_event_payload if event_type == "adventure.completed" else None,
            outbox_event_type="dm.turn_committed",
            outbox_payload={**base_payload, "operation_id": duplicate_op},
            outbox_operation_id=duplicate_op,
        )
    except RevisionConflictError as exc:
        logger.warning(
            "dm_turn revision_conflict_on_commit campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s expected=%s actual=%s",
            turn.campaign_id, turn.thread_id, turn.id, attempt.id, exc.expected_revision, exc.actual_revision,
        )
        db.rollback()
        # Re-lock after rollback
        try:
            turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
            attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
        except Exception:
            turn = db.get(DmTurn, turn_id)
            attempt = db.get(DmTurnAttempt, attempt_id)
        if attempt and turn:
            attempt.last_error = str(exc)
            attempt.error_class = "revision_conflict"
            attempt.completed_at = _now()
            attempt.status = ATTEMPT_FAILED_VISIBLE
            turn.status = TURN_FAILED_VISIBLE
            try:
                db.flush()
                if commit:
                    db.commit()
            except Exception:
                db.rollback()
        raise StaleRevisionError(turn.campaign_id, exc.expected_revision, exc.actual_revision, attempt.id) from exc

    now = _now()
    commit_duration_ms = int((time.monotonic() - execute_start) * 1000)
    # Link the authoritative event back onto the completed adventure for
    # turn/event provenance (issue #260). Strictly additive bookkeeping —
    # never breaks the commit.
    if event_type == "adventure.completed":
        try:
            from models.campaigns import Adventure as _Adventure

            _completed_adv = None
            _explicit_aid = (base_payload.get("adventure_completion") or {}).get("adventure_id")
            if _explicit_aid:
                try:
                    _completed_adv = db.get(_Adventure, uuid.UUID(str(_explicit_aid)))
                except ValueError:
                    _completed_adv = None
            if _completed_adv is None:
                _completed_adv = (
                    db.execute(
                        select(_Adventure).where(
                            _Adventure.campaign_id == turn.campaign_id,
                            _Adventure.status == "completed",
                            _Adventure.source_turn_id == turn.id,
                        )
                    )
                    .scalars()
                    .first()
                )
            if _completed_adv is not None and _completed_adv.source_event_id is None:
                _completed_adv.source_event_id = event.id
                db.flush()
                # Shared #263 finalization, post-commit: the authoritative
                # completion event and campaign revision exist only now, so
                # the end cursor binds exactly (event.sequence ==
                # campaign revision by invariant). Best-effort: derived-work
                # failures are recorded, never break the turn commit.
                try:
                    from app.adventures.service import (
                        finalize_adventure_derived as _finalize,
                    )

                    _finalize(
                        db, _completed_adv,
                        event_sequence=event.sequence,
                        revision=campaign_after.revision,
                    )
                    db.flush()
                except Exception as e:
                    logger.warning(
                        "dm_turn failed to finalize adventure summary turn_id=%s error=%s",
                        turn.id, e,
                    )
        except Exception as e:
            logger.warning("dm_turn failed to link adventure event turn_id=%s error=%s", turn.id, e)
    # Stage encounter.started lifecycle semantics for encounters created by
    # this attempt's start_encounter effect (issue #230). The turn commit IS
    # the start's fictional mutation, so each linked encounter gets its own
    # domain event + durable outbox hook chained in the same outer
    # transaction (one event per revision, preserving the
    # sequence == revision invariant). Fail-closed: any staging failure
    # propagates and aborts the turn commit — a durable encounter without
    # its lifecycle event must never commit. Direct realtime delivery
    # happens post-commit below.
    linked_encounter_ids: list[uuid.UUID] = []
    from models.campaigns import CampaignDomainEvent as _DomainEvent
    from models.combat import Encounter as _Encounter

    _started = db.execute(
        select(_Encounter).where(
            _Encounter.campaign_id == turn.campaign_id,
            _Encounter.source_attempt_id == attempt.id,
            _Encounter.created_event_id.is_(None),
        )
    ).scalars().all()
    if _started:
        from app.campaigns.events import commit_campaign_mutation as _commit_mutation
        from app.combat.service import (
            ENCOUNTER_STARTED_EVENT as _ENCOUNTER_STARTED,
            list_participants as _list_parts,
        )

        for _enc in _started:
            _lifecycle = db.execute(
                select(_DomainEvent).where(
                    _DomainEvent.campaign_id == turn.campaign_id,
                    _DomainEvent.operation_id == _enc.operation_id,
                    _DomainEvent.event_type == _ENCOUNTER_STARTED,
                )
            ).scalars().first()
            if _lifecycle is None:
                _, _lifecycle = _commit_mutation(
                    db,
                    turn.campaign_id,
                    expected_revision=int(campaign_after.revision or 0),
                    event_type=_ENCOUNTER_STARTED,
                    payload={
                        "encounter_id": str(_enc.id),
                        "thread_id": _enc.thread_id,
                        "participant_count": int(_enc.participant_count or 0),
                        "start_source": _enc.start_source,
                        "source_turn_id": str(turn.id),
                        "source_attempt_id": str(attempt.id),
                        "participants": [
                            {"id": str(p.id), "kind": p.kind, "display_name": p.display_name}
                            for p in _list_parts(db, _enc.id)
                        ],
                    },
                    operation_id=_enc.operation_id,
                    actor_id=event.actor_id,
                    provenance={
                        "source": "dm_effect",
                        "turn_event_id": str(event.id),
                        "attempt_id": str(attempt.id),
                    },
                    outbox_event_type=_ENCOUNTER_STARTED,
                    outbox_payload={
                        "encounter_id": str(_enc.id),
                        "campaign_id": str(turn.campaign_id),
                        "thread_id": _enc.thread_id,
                        "participant_count": int(_enc.participant_count or 0),
                        "start_source": _enc.start_source,
                    },
                    outbox_operation_id=f"encounter:{_enc.id}:started",
                    commit=False,
                )
            _enc.created_event_id = _lifecycle.id
            linked_encounter_ids.append(_enc.id)
        db.flush()
    # Stage encounter.ended lifecycle semantics for encounters closed by this
    # attempt's end_encounter effect (issue #239). Mirrors the start staging
    # above: the turn commit IS the end's fictional mutation, so each
    # inline-ended encounter without a staged event gets its own domain event
    # + durable outbox hook in the same outer transaction. Fail-closed like
    # the start path — an unstaged durable end must never commit. The API end
    # path stages its own event immediately (ended_event_id set), so the
    # IS NULL scope only catches inline ends from this commit.
    linked_ended_ids: list[uuid.UUID] = []
    _ended = db.execute(
        select(_Encounter).where(
            _Encounter.campaign_id == turn.campaign_id,
            _Encounter.status == "ended",
            _Encounter.ended_event_id.is_(None),
        )
    ).scalars().all()
    if _ended:
        from app.campaigns.events import commit_campaign_mutation as _commit_end_mutation
        from app.combat.ending import build_final_snapshot as _final_snapshot
        from app.combat.ending import list_end_followups as _end_hooks
        from app.combat.service import ENCOUNTER_ENDED_EVENT as _ENCOUNTER_ENDED

        for _enc in _ended:
            _end_lifecycle = db.execute(
                select(_DomainEvent).where(
                    _DomainEvent.campaign_id == turn.campaign_id,
                    _DomainEvent.operation_id == _enc.end_operation_id,
                    _DomainEvent.event_type == _ENCOUNTER_ENDED,
                )
            ).scalars().first()
            if _end_lifecycle is None:
                _, _end_lifecycle = _commit_end_mutation(
                    db,
                    turn.campaign_id,
                    expected_revision=int(campaign_after.revision or 0),
                    event_type=_ENCOUNTER_ENDED,
                    payload={
                        "encounter_id": str(_enc.id),
                        "thread_id": _enc.thread_id,
                        "outcome": _enc.end_outcome,
                        "reason": _enc.end_reason,
                        "round": int(_enc.round or 1),
                        "turn_sequence": int(_enc.turn_sequence or 0),
                        "duration_ms": int(_enc.end_duration_ms or 0),
                        "participant_outcomes": dict(_enc.end_participant_outcomes or {}),
                        "followup_hooks": [h.hook_type for h in _end_hooks(db, _enc.id)],
                        "final_state": _final_snapshot(db, _enc),
                        "ended_by": str(_enc.ended_by) if _enc.ended_by else None,
                    },
                    operation_id=_enc.end_operation_id,
                    # Issue #239 privacy: owner-only like the API path.
                    # event.actor_id may be the player whose turn triggered
                    # the DM effect; the ended payload carries DM-private
                    # reason/fates, so the campaign owner (AI-DM path) must
                    # own the event or members would see it as own-actor.
                    actor_id=campaign_after.owner_id,
                    visibility="dm_only",
                    provenance={
                        "source": "dm_effect",
                        "turn_event_id": str(event.id),
                        "attempt_id": str(attempt.id),
                    },
                    outbox_event_type=_ENCOUNTER_ENDED,
                    outbox_payload={
                        "encounter_id": str(_enc.id),
                        "campaign_id": str(turn.campaign_id),
                        "thread_id": _enc.thread_id,
                        "outcome": _enc.end_outcome,
                    },
                    outbox_operation_id=f"encounter:{_enc.id}:ended",
                    commit=False,
                )
            _enc.ended_event_id = _end_lifecycle.id
            linked_ended_ids.append(_enc.id)
        db.flush()
    turn.status = TURN_SUCCEEDED
    turn.resolved_at = now
    turn.committed_at = now
    turn.commit_duration_ms = commit_duration_ms
    turn.time_executing_ms = commit_duration_ms
    attempt.status = ATTEMPT_SUCCEEDED
    attempt.completed_at = now
    attempt.result = event.to_dict() if hasattr(event, "to_dict") else {"event_id": str(event.id)}
    attempt.processing_duration_ms = commit_duration_ms
    attempt.last_error = None
    attempt.error_class = None

    if attempt.submission_ids:
        try:
            sub_uuids = [uuid.UUID(s) for s in (attempt.submission_ids or [])]
            rows = db.execute(
                select(PlayerSubmission).where(PlayerSubmission.id.in_(sub_uuids))
            ).scalars().all()
            for row in rows:
                row.resolution_status = "resolved"
                row.resolved_at = now
        except Exception as e:
            logger.warning("dm_turn failed to resolve submissions turn_id=%s error=%s", turn.id, e)

    # Mark stream completed if linked
    if attempt.stream_id:
        try:
            from models.dm import DMStream
            stream = db.get(DMStream, attempt.stream_id)
            if stream and stream.status == "streaming":
                stream.status = "completed"
                stream.completed_at = now
                stream.completion_reason = "turn_committed"
        except Exception as e:
            logger.warning("dm_turn failed to complete stream turn_id=%s stream_id=%s error=%s", turn.id, attempt.stream_id, e)

    db.flush()
    if commit:
        db.commit()
    db.refresh(turn)
    db.refresh(attempt)
    db.refresh(campaign_after)
    db.refresh(event)

    # Post-commit encounter-start realtime hook (issue #230). The durable
    # outbox row staged above is the guaranteed delivery path; this direct
    # publish is latency-only and best-effort — it never rolls back
    # committed state.
    if commit and linked_encounter_ids:
        try:
            from app.realtime.service import publish_encounter_started as _publish_started
            from models.combat import Encounter as _EncounterPub

            for _eid in linked_encounter_ids:
                _row = db.get(_EncounterPub, _eid)
                if _row is not None:
                    _publish_started(db, _row)
        except Exception as e:
            logger.warning("dm_turn encounter post-commit publish skipped turn_id=%s error=%s", turn.id, e)

    # Post-commit encounter-ended realtime hook (issue #239). Same contract
    # as the start hook above: durable outbox owns delivery, this is
    # latency-only and never rolls back committed state.
    if commit and linked_ended_ids:
        try:
            from app.realtime.service import publish_encounter_ended as _publish_ended

            for _eid in linked_ended_ids:
                _row = db.get(_EncounterPub, _eid)
                if _row is not None:
                    _publish_ended(db, _row)
        except Exception as e:
            logger.warning("dm_turn encounter-end post-commit publish skipped turn_id=%s error=%s", turn.id, e)

    # Post-commit #213 semantic-index hook for turn-path writes. Staged
    # assert_fact / upsert_relation effects (and JIT-promoted entities) use
    # the *_inline writers inside the turn transaction, so they bypass the
    # *_authoritative hooks — without this, committed turn records would
    # never become searchable. Best-effort derived work only: never breaks
    # the committed turn. Skipped when the caller owns the transaction
    # (commit=False), mirroring the encounter hook above.
    if commit:
        try:
            from app.world import semantic as _semantic

            _semantic.note_turn_committed(
                db, turn.campaign_id, turn.id, attempt.id, event_id=event.id)
        except Exception as e:
            logger.warning("dm_turn semantic index hook skipped turn_id=%s error=%s", turn.id, e)

    # Post-commit identity-telemetry flush (issue #214). Records collected
    # during locked JIT promotion persist only now that the campaign lock
    # is released and the entities are durable. Fail-soft: never breaks
    # the committed turn. Skipped when the caller owns the transaction
    # (commit=False), mirroring the hooks above.
    if commit and identity_telemetry_outbox:
        try:
            from app.decisions import record_fail_soft as _record_fail_soft

            from database import SessionLocal as _SessionLocal

            for _record in identity_telemetry_outbox:
                _record_fail_soft(_SessionLocal, _record)
        except Exception as e:
            logger.warning("dm_turn identity telemetry flush skipped turn_id=%s error=%s", turn.id, e)

    logger.info(
        "dm_turn committed campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s new_revision=%s event_id=%s "
        "input_set_revision=%s submission_count=%s assembly_window_start=%s assembly_window_end=%s time_executing_ms=%s "
        "staged_effect_count=%s staged_effect_types=%s commit_duration_ms=%s operation_id=%s audience=%s visibility=%s",
        turn.campaign_id, turn.thread_id, turn.id, attempt.id, campaign_after.revision, event.id,
        attempt.input_set_revision, len(attempt.submission_ids or []),
        turn.assembly_window_start.isoformat() if turn.assembly_window_start else None,
        turn.assembly_window_end.isoformat() if turn.assembly_window_end else None,
        turn.time_executing_ms,
        len(staged_list), [e.get("effect_type") for e in staged_list], commit_duration_ms, duplicate_op,
        attempt_audience, turn_visibility,
    )
    return turn, attempt, event


def commit_turn_with_effects(
    db: Session,
    turn_id: uuid.UUID,
    attempt_id: uuid.UUID,
    expected_revision: int | None = None,
    event_type: str = "dm.turn_resolved",
    payload: dict | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    commit: bool = True,
) -> tuple[DmTurn, DmTurnAttempt, Any]:
    """Thin wrapper for staged-effects commit (issue #206). Delegates to commit_turn."""
    return commit_turn(db, turn_id, attempt_id, expected_revision=expected_revision, mutate=None, event_type=event_type, payload=payload, operation_id=operation_id, actor_id=actor_id, commit=commit)


def abandon_visible_attempt(
    db: Session,
    turn_id: uuid.UUID,
    attempt_id: uuid.UUID,
    reason: str = "explicit_retry",
    actor_id: uuid.UUID | None = None,
) -> tuple[DmTurn, DmTurnAttempt]:
    """Abandon a visible partial attempt without mutating authoritative state (explicit Retry).

    Only streaming/failed_visible attempts can be abandoned. Staged effects remain
    for audit but are never promoted. The stream is marked abandoned/non-canonical.
    After abandon, the blocking turn no longer prevents next turn advancement.
    Idempotent.
    """
    try:
        turn = db.execute(select(DmTurn).where(DmTurn.id == turn_id).with_for_update()).scalars().first()
        attempt = db.execute(select(DmTurnAttempt).where(DmTurnAttempt.id == attempt_id).with_for_update()).scalars().first()
    except Exception:
        turn = db.get(DmTurn, turn_id)
        attempt = db.get(DmTurnAttempt, attempt_id)
    if turn is None or attempt is None:
        raise ValueError(f"Turn {turn_id} or attempt {attempt_id} not found")
    if str(attempt.turn_id) != str(turn.id):
        raise ValueError(f"Attempt {attempt_id} does not belong to turn {turn_id}")
    if str(turn.current_attempt_id) != str(attempt_id):
        raise ValueError(f"Attempt {attempt_id} is not current for turn {turn_id}")

    if attempt.status == ATTEMPT_ABANDONED and turn.status == TURN_ABANDONED:
        return turn, attempt

    # Only visible attempts can be abandoned
    if attempt.status not in (ATTEMPT_STREAMING, ATTEMPT_FAILED_VISIBLE) or turn.status not in (TURN_STREAMING, TURN_FAILED_VISIBLE):
        raise ValueError(f"Cannot abandon attempt {attempt_id} with status {attempt.status} / turn {turn_id} status {turn.status}; must be streaming/failed_visible")

    now = _now()
    # Compute visible-but-incomplete duration for observability
    visible_ms = None
    if attempt.streaming_started_at:
        try:
            visible_ms = int((now - attempt.streaming_started_at).total_seconds() * 1000)
        except Exception:
            visible_ms = None
    elif turn.streaming_started_at:
        try:
            visible_ms = int((now - turn.streaming_started_at).total_seconds() * 1000)
        except Exception:
            visible_ms = None

    attempt.status = ATTEMPT_ABANDONED
    attempt.abandoned_at = now
    attempt.abandonment_reason = reason[:64] if reason else "explicit_retry"
    attempt.completed_at = now
    attempt.last_error = f"Abandoned visible attempt: {reason}"
    attempt.error_class = "abandoned"

    turn.status = TURN_ABANDONED
    turn.abandoned_at = now
    turn.abandonment_reason = reason[:64] if reason else "explicit_retry"

    # Mark stream abandoned
    if attempt.stream_id:
        try:
            from models.dm import DMStream

            stream = db.get(DMStream, attempt.stream_id)
            if stream:
                stream.status = "abandoned"
                stream.abandoned_at = now
                stream.abandonment_reason = reason[:64] if reason else "explicit_retry"
        except Exception as e:
            logger.warning("abandon failed to mark stream abandoned turn_id=%s stream_id=%s error=%s", turn_id, attempt.stream_id, e)

    db.flush()
    db.commit()
    db.refresh(turn)
    db.refresh(attempt)

    logger.info(
        "dm_turn abandoned campaign_id=%s thread_id=%s turn_id=%s attempt_id=%s reason=%s staged_effect_count=%s time_visible_but_incomplete_ms=%s",
        turn.campaign_id, turn.thread_id, turn.id, attempt.id, reason, len(attempt.staged_effects or []), visible_ms,
    )
    return turn, attempt


def discard_superseded_result(db: Session, attempt_id: uuid.UUID, reason: str = "superseded") -> DmTurnAttempt | None:
    """Handle an obsolete attempt that finished model execution after supersession."""
    attempt = db.get(DmTurnAttempt, attempt_id)
    if attempt is None:
        return None
    turn = db.get(DmTurn, attempt.turn_id) if attempt.turn_id else None
    if turn and str(turn.current_attempt_id) == str(attempt_id) and attempt.status not in (ATTEMPT_SUPERSEDED, ATTEMPT_DISCARDED):
        logger.info("dm_turn discard_skipped_still_current attempt_id=%s turn_id=%s status=%s", attempt_id, attempt.turn_id, attempt.status)
        return attempt
    if attempt.status in (ATTEMPT_SUCCEEDED, ATTEMPT_FAILED_VISIBLE):
        return attempt
    attempt.status = ATTEMPT_DISCARDED
    attempt.last_error = f"Discarded obsolete attempt: {reason}"
    attempt.error_class = "superseded"
    attempt.completed_at = _now()
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
    """Mark attempt as failed; if visible, turn becomes failed_visible and blocks advancing."""
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


def recover_stuck_attempts(
    db: Session, *, campaign_id: uuid.UUID | None = None, lease_seconds: int = 300, commit: bool = True
) -> int:
    """Recover attempts left in running without completion (worker crash).

    When ``campaign_id`` is given, only attempts for that campaign are recovered
    (prevents cross-campaign reset from a path-scoped recover endpoint).
    """
    cutoff = _now() - timedelta(seconds=lease_seconds)
    q = select(DmTurnAttempt).where(
        DmTurnAttempt.status == ATTEMPT_RUNNING,
        DmTurnAttempt.started_at < cutoff,
    )
    if campaign_id is not None:
        q = q.where(DmTurnAttempt.campaign_id == campaign_id)
    candidates = db.execute(q).scalars().all()
    count = 0
    for attempt in candidates:
        # An expired timestamp alone never authorizes resetting a live executor.
        if db.get_bind().dialect.name == "postgresql":
            from app.dm.ownership import try_execution_lock
            if not try_execution_lock(db, attempt.id):
                continue
        turn = db.get(DmTurn, attempt.turn_id)
        if turn and turn.status == TURN_STREAMING:
            continue
        recovered = db.execute(
            update(DmTurnAttempt)
            .where(DmTurnAttempt.id == attempt.id,
                   DmTurnAttempt.status == ATTEMPT_RUNNING,
                   DmTurnAttempt.started_at < cutoff)
            .values(status=ATTEMPT_PREPARED, started_at=None, worker_job_id=None,
                    last_error=f"Recovered stuck running attempt after {lease_seconds}s lease expiry")
            .execution_options(synchronize_session=False)
        )
        if recovered.rowcount:
            db.expire(attempt)
            count += 1
            logger.info("dm_turn recovered_stuck attempt_id=%s turn_id=%s", attempt.id, attempt.turn_id)
    if commit:
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
