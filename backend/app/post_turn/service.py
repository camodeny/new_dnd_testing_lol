"""Post-turn durable checkpoint, batching policy, cumulative catch-up — issue #216.

Batching is policy, not authority: the checkpoint advances only after the
required consolidation for a range succeeds. Failed ranges stay outstanding
and the next run cumulatively covers checkpoint+1 through the current
eligible sequence, so nothing accepted is silently dropped.

Transport: triggers enqueue through the transactional outbox (#190) with the
run id as outbox/job id; the relay publishes to the queue and the worker
executes idempotently via WorkerExecution fencing (#191).

The actual memory/clock/repair contents of a post-turn patch are out of
scope — consolidation here validates the range against the immutable
campaign sequence, preserves visibility metadata on read, and records the
processed span. Content builders plug in later behind ``consolidate_fn``.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from collections.abc import Callable

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.campaigns import Campaign, CampaignDomainEvent
from models.post_turn import PostTurnCheckpoint, PostTurnRun

logger = logging.getLogger(__name__)

POST_TURN_JOB_TYPE = "post_turn.process"

NORMAL = "normal"
FORCE = "force"
CRITICAL = "critical"
ADMIN_SKIP = "admin_skip"

DEFAULT_BATCH_SIZE = 4

# By default every committed domain event is post-turn relevant: nothing
# accepted may be silently dropped. Specific operational event types can be
# excluded via POST_TURN_IRRELEVANT_TYPES (comma-separated) without changing
# schema semantics — the checkpoint stays sequence-based so excluded types
# still advance the checkpoint as part of their contiguous range.
IRRELEVANT_EVENT_TYPES: frozenset[str] = frozenset(
    t.strip() for t in os.getenv("POST_TURN_IRRELEVANT_TYPES", "").split(",") if t.strip()
)


def get_batch_threshold() -> int:
    """Configurable batch trigger count (default ~4 relevant events)."""
    try:
        return max(1, int(os.getenv("POST_TURN_BATCH_SIZE", str(DEFAULT_BATCH_SIZE))))
    except (TypeError, ValueError):
        return DEFAULT_BATCH_SIZE


def is_post_turn_relevant(event_type: str | None) -> bool:
    """Relevant-event predicate over the immutable campaign sequence."""
    if not event_type:
        return False
    return event_type.strip() not in IRRELEVANT_EVENT_TYPES


# ── Checkpoint / sequence helpers ──────────────────────────────────────────


def get_checkpoint(db: Session, campaign_id: uuid.UUID, *, commit: bool = True) -> PostTurnCheckpoint:
    rec = db.get(PostTurnCheckpoint, campaign_id)
    if rec is not None:
        return rec
    rec = PostTurnCheckpoint(campaign_id=campaign_id, processed_through_sequence=0)
    db.add(rec)
    try:
        db.flush()
        if commit:
            db.commit()
            db.refresh(rec)
    except IntegrityError:
        db.rollback()
        existing = db.get(PostTurnCheckpoint, campaign_id)
        if existing is None:
            raise
        return existing
    return rec


def get_max_sequence(db: Session, campaign_id: uuid.UUID) -> int:
    mx = db.execute(
        select(func.max(CampaignDomainEvent.sequence)).where(CampaignDomainEvent.campaign_id == campaign_id)
    ).scalar()
    if mx is not None:
        return int(mx)
    camp = db.get(Campaign, campaign_id)
    if camp is not None and camp.revision is not None:
        return int(camp.revision)
    return 0


def get_outstanding_range(db: Session, campaign_id: uuid.UUID) -> dict:
    """Cumulative range: checkpoint+1 through current eligible sequence."""
    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    max_seq = get_max_sequence(db, campaign_id)
    from_seq = processed + 1
    if max_seq < from_seq:
        return {
            "from_sequence": from_seq, "to_sequence": max_seq,
            "outstanding": 0, "relevant": 0,
            "processed_through": processed, "max_sequence": max_seq,
            "age_seconds": 0.0,
        }
    rows = db.execute(
        select(CampaignDomainEvent.event_type, CampaignDomainEvent.created_at)
        .where(CampaignDomainEvent.campaign_id == campaign_id,
               CampaignDomainEvent.sequence >= from_seq,
               CampaignDomainEvent.sequence <= max_seq)
        .order_by(CampaignDomainEvent.sequence.asc())
    ).all()
    relevant = sum(1 for et, _ in rows if is_post_turn_relevant(et))
    age = 0.0
    if rows:
        oldest = rows[0][1]
        if oldest is not None:
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=timezone.utc)
            age = max(0.0, (datetime.now(timezone.utc) - oldest).total_seconds())
    return {
        "from_sequence": from_seq, "to_sequence": max_seq,
        "outstanding": max_seq - from_seq + 1, "relevant": relevant,
        "processed_through": processed, "max_sequence": max_seq,
        "age_seconds": age,
    }


def should_trigger_post_turn(outstanding_relevant: int, trigger: str = NORMAL, *, threshold: int | None = None) -> bool:
    if outstanding_relevant <= 0:
        return False
    if trigger in (FORCE, CRITICAL, ADMIN_SKIP):
        return True
    th = threshold if threshold is not None else get_batch_threshold()
    return outstanding_relevant >= th


# ── Trigger (outbox emission) ──────────────────────────────────────────────


def maybe_trigger_post_turn(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    trigger: str = NORMAL,
    operation_id: str | None = None,
    trace_id: str | None = None,
    threshold: int | None = None,
    commit: bool = True,
) -> PostTurnRun | None:
    """Evaluate the batch policy and enqueue a cumulative post-turn run via outbox.

    Returns the run (new or existing duplicate) or None when below threshold.
    The run id is reused as the outbox id / worker job_id for end-to-end
    idempotency.
    """
    if trigger not in (NORMAL, FORCE, CRITICAL):
        raise ValueError(f"unknown trigger {trigger!r}")
    span = get_outstanding_range(db, campaign_id)
    if span["outstanding"] <= 0:
        return None
    if not should_trigger_post_turn(span["relevant"], trigger, threshold=threshold):
        return None
    from_seq, to_seq = span["from_sequence"], span["to_sequence"]

    # Duplicate trigger for the same logical range reuses the existing run.
    existing = db.execute(
        select(PostTurnRun).where(
            PostTurnRun.campaign_id == campaign_id,
            PostTurnRun.from_sequence == from_seq,
            PostTurnRun.to_sequence == to_seq,
        )
    ).scalars().first()
    if existing is not None:
        logger.info("post_turn trigger duplicate campaign=%s %s-%s run=%s", campaign_id, from_seq, to_seq, existing.id)
        return existing

    from app.observability.tracing import current_trace_id

    run = PostTurnRun(
        id=uuid.uuid4(),
        campaign_id=campaign_id,
        from_sequence=from_seq,
        to_sequence=to_seq,
        trigger=trigger,
        status="pending",
        attempts=0,
        operation_id=operation_id,
        trace_id=trace_id or current_trace_id(),
        idempotency_key=f"post-turn:{campaign_id}:{from_seq}-{to_seq}",
    )
    db.add(run)
    db.flush()

    # Atomic outbox emission in the same transaction (issue #190 pattern).
    from models.reliability import Outbox as _Outbox

    ob = _Outbox(
        id=run.id,
        aggregate_type="campaign",
        aggregate_id=campaign_id,
        campaign_id=campaign_id,
        event_type=POST_TURN_JOB_TYPE,
        operation_id=operation_id,
        trace_id=run.trace_id,
        payload={
            "run_id": str(run.id),
            "campaign_id": str(campaign_id),
            "from_sequence": from_seq,
            "to_sequence": to_seq,
            "trigger": trigger,
        },
        status="pending",
        attempts=0,
    )
    db.add(ob)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race: outbox id (== run id) already emitted or range row
        # already exists — reuse the existing run.
        db.rollback()
        dup = db.execute(
            select(PostTurnRun).where(
                PostTurnRun.campaign_id == campaign_id,
                PostTurnRun.from_sequence == from_seq,
                PostTurnRun.to_sequence == to_seq,
            )
        ).scalars().first()
        if dup is not None:
            return dup
        raise
    if commit:
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            dup = db.execute(
                select(PostTurnRun).where(
                    PostTurnRun.campaign_id == campaign_id,
                    PostTurnRun.from_sequence == from_seq,
                    PostTurnRun.to_sequence == to_seq,
                )
            ).scalars().first()
            if dup is not None:
                return dup
            raise
        db.refresh(run)
    logger.info("post_turn triggered campaign=%s %s-%s trigger=%s run=%s", campaign_id, from_seq, to_seq, trigger, run.id)
    return run


# ── Consolidation (worker side) ────────────────────────────────────────────


def _validate_range_contiguous(db: Session, campaign_id: uuid.UUID, from_seq: int, to_seq: int) -> list[CampaignDomainEvent]:
    """Load the range and fail loudly on gaps — never silently skip."""
    events = list(
        db.execute(
            select(CampaignDomainEvent)
            .where(CampaignDomainEvent.campaign_id == campaign_id,
                   CampaignDomainEvent.sequence >= from_seq,
                   CampaignDomainEvent.sequence <= to_seq)
            .order_by(CampaignDomainEvent.sequence.asc())
        ).scalars().all()
    )
    seqs = [e.sequence for e in events]
    expected = list(range(from_seq, to_seq + 1))
    if seqs != expected:
        missing = sorted(set(expected) - set(seqs))
        raise RuntimeError(f"post-turn range {from_seq}-{to_seq} has gaps (missing={missing}); refusing to advance checkpoint")
    # Preserve visibility metadata on read (security: DM-private evidence
    # stays labelled; this phase only reads).
    for e in events:
        if not e.visibility:
            raise RuntimeError(f"post-turn event {e.sequence} missing visibility metadata")
    return events


def run_post_turn_range(
    db: Session,
    campaign_id: uuid.UUID,
    from_sequence: int,
    to_sequence: int,
    *,
    run_id: uuid.UUID | None = None,
    operation_id: str | None = None,
    consolidate_fn: Callable[[list[CampaignDomainEvent]], dict] | None = None,
    commit: bool = True,
) -> dict:
    """Consolidate one range and advance the checkpoint (forward-only).

    Idempotent: if to_sequence <= checkpoint, returns duplicate without
    side effects. On failure the checkpoint is unchanged and the run is
    marked failed with the reason.
    """
    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    if to_sequence <= processed:
        logger.info("post_turn duplicate campaign=%s range=%s-%s checkpoint=%s", campaign_id, from_sequence, to_sequence, processed)
        if run_id is not None:
            run = db.get(PostTurnRun, run_id)
            if run is not None and run.status != "succeeded":
                run.status = "succeeded"
                run.completed_at = datetime.now(timezone.utc)
                if commit:
                    db.commit()
        return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": processed}

    run: PostTurnRun | None = db.get(PostTurnRun, run_id) if run_id is not None else None
    if run is None:
        # Worker redelivery without a trigger record (e.g. direct envelope):
        # create the durable run row keyed by job/run id when available.
        run = PostTurnRun(
            id=run_id or uuid.uuid4(),
            campaign_id=campaign_id,
            from_sequence=from_sequence,
            to_sequence=to_sequence,
            trigger=NORMAL,
            status="running",
            operation_id=operation_id,
            idempotency_key=f"post-turn:{campaign_id}:{from_sequence}-{to_sequence}",
        )
        db.add(run)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            # Same logical range already recorded — treat as duplicate path.
            dup = db.execute(
                select(PostTurnRun).where(
                    PostTurnRun.campaign_id == campaign_id,
                    PostTurnRun.from_sequence == from_sequence,
                    PostTurnRun.to_sequence == to_sequence,
                )
            ).scalars().first()
            if dup is not None and dup.status == "succeeded":
                return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                        "processed_through": processed}
            run = dup
    if run is not None and run.status not in ("succeeded", "skipped"):
        run.status = "running"
        run.attempts = (run.attempts or 0) + 1
        db.flush()

    try:
        events = _validate_range_contiguous(db, campaign_id, from_sequence, to_sequence)
        if consolidate_fn is not None:
            patch = consolidate_fn(events)
        else:
            # Placeholder consolidation (content out of scope): record span.
            patch = {"processed_span": [from_sequence, to_sequence], "event_count": len(events)}
        if not isinstance(patch, dict):
            raise RuntimeError("consolidate_fn must return a dict")

        # Forward-only checkpoint advancement (rollback safety: never moves back).
        result = db.execute(
            update(PostTurnCheckpoint)
            .where(PostTurnCheckpoint.campaign_id == campaign_id,
                   PostTurnCheckpoint.processed_through_sequence < to_sequence)
            .values(processed_through_sequence=to_sequence,
                    updated_by_run_id=run.id if run is not None else None,
                    updated_at=datetime.now(timezone.utc))
        )
        if run is not None:
            run.status = "succeeded"
            run.failure_reason = None
            run.result = patch
            run.completed_at = datetime.now(timezone.utc)
            db.add(run)
        if commit:
            db.commit()
        logger.info("post_turn consolidated campaign=%s %s-%s run=%s advanced=%s",
                    campaign_id, from_sequence, to_sequence, run.id if run else "-", bool(result.rowcount))
        return {"duplicate": False, "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": to_sequence, "event_count": len(events), "result": patch}
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        # Re-fetch run in the fresh transaction before marking failed so the
        # failure reason is durable even though the checkpoint is unchanged.
        if run is not None:
            fresh = db.get(PostTurnRun, run.id)
            if fresh is not None:
                if fresh.status != "succeeded":
                    fresh.status = "failed"
                    fresh.failure_reason = f"{type(exc).__name__}: {exc}"[:2000]
                    try:
                        if commit:
                            db.commit()
                        else:
                            db.flush()
                    except Exception:
                        try:
                            db.rollback()
                        except Exception:
                            pass
                run = fresh
        logger.warning("post_turn failed campaign=%s %s-%s error=%s", campaign_id, from_sequence, to_sequence, exc)
        raise


def handle_post_turn_envelope(envelope, db: Session | None = None) -> dict:
    """Queue-worker handler for ``post_turn.process`` envelopes.

    Single-argument worker contract (see execute_worker_job); owns its DB
    session via SessionLocal with a ``db`` seam for tests.
    """
    payload = getattr(envelope, "payload", None) or {}
    try:
        campaign_id = uuid.UUID(str(payload["campaign_id"]))
    except Exception:
        raise ValueError("post_turn.process envelope payload must include campaign_id")
    try:
        from_seq = int(payload["from_sequence"])
        to_seq = int(payload["to_sequence"])
    except Exception:
        raise ValueError("post_turn.process envelope payload must include from_sequence/to_sequence")
    raw_run = payload.get("run_id")
    run_id = uuid.UUID(str(raw_run)) if raw_run else getattr(envelope, "job_id", None)

    if db is not None:
        return run_post_turn_range(db, campaign_id, from_seq, to_seq, run_id=run_id,
                                   operation_id=getattr(envelope, "operation_id", None))
    from database import SessionLocal

    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not configured")
    with SessionLocal() as session:
        return run_post_turn_range(session, campaign_id, from_seq, to_seq, run_id=run_id,
                                   operation_id=getattr(envelope, "operation_id", None))


def register_post_turn_worker() -> None:
    from app.queue.consumer import WORKER_HANDLERS

    WORKER_HANDLERS[POST_TURN_JOB_TYPE] = handle_post_turn_envelope


register_post_turn_worker()


# ── Explicit audited skip ──────────────────────────────────────────────────


def mark_post_turn_skipped(
    db: Session,
    campaign_id: uuid.UUID,
    to_sequence: int,
    reason: str,
    *,
    actor_id: uuid.UUID | None = None,
    commit: bool = True,
) -> PostTurnRun:
    """Explicit audited decision to skip ahead — the only way to permanently
    skip a committed sequence. Requires a non-empty reason."""
    if not reason or not reason.strip():
        raise ValueError("an explicit audited reason is required to skip post-turn sequences")
    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    if to_sequence <= processed:
        raise ValueError(f"to_sequence {to_sequence} does not advance checkpoint {processed}")
    max_seq = get_max_sequence(db, campaign_id)
    if to_sequence > max_seq:
        raise ValueError(f"to_sequence {to_sequence} exceeds max committed sequence {max_seq}")
    run = PostTurnRun(
        id=uuid.uuid4(),
        campaign_id=campaign_id,
        from_sequence=processed + 1,
        to_sequence=to_sequence,
        trigger=ADMIN_SKIP,
        status="skipped",
        operation_id=f"admin-skip:{actor_id}" if actor_id else "admin-skip",
        result={"reason": reason.strip(), "actor_id": str(actor_id) if actor_id else None},
        completed_at=datetime.now(timezone.utc),
    )
    db.add(run)
    db.flush()
    db.execute(
        update(PostTurnCheckpoint)
        .where(PostTurnCheckpoint.campaign_id == campaign_id,
               PostTurnCheckpoint.processed_through_sequence < to_sequence)
        .values(processed_through_sequence=to_sequence, updated_by_run_id=run.id,
                updated_at=datetime.now(timezone.utc))
    )
    if commit:
        db.commit()
        db.refresh(run)
    logger.warning("post_turn admin_skip campaign=%s to=%s reason=%s", campaign_id, to_sequence, reason.strip()[:200])
    return run


# ── Observability ──────────────────────────────────────────────────────────


def get_post_turn_status(db: Session, campaign_id: uuid.UUID) -> dict:
    cp = get_checkpoint(db, campaign_id, commit=False)
    span = get_outstanding_range(db, campaign_id)
    runs = list(
        db.execute(
            select(PostTurnRun).where(PostTurnRun.campaign_id == campaign_id)
            .order_by(PostTurnRun.created_at.desc()).limit(20)
        ).scalars().all()
    )
    failed = [r for r in runs if r.status == "failed"]
    last = runs[0] if runs else None
    retry_count = sum(int(r.attempts or 0) for r in runs)
    return {
        "campaign_id": str(campaign_id),
        "checkpoint": int(cp.processed_through_sequence or 0),
        "outstanding": span,
        "run_attempts": len(runs),
        "retry_count": retry_count,
        "last_run": last.to_dict() if last else None,
        "last_failure_reason": failed[0].failure_reason if failed else None,
        "failed_runs": len(failed),
        "queue_lag_seconds": span.get("age_seconds", 0.0),
    }
