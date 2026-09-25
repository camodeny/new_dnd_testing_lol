"""Post-turn durable checkpoint, batching policy, cumulative catch-up — issue #216.

Batching is policy, not authority: the checkpoint advances only after the
required consolidation for a range succeeds. Failed ranges stay outstanding
and the next run cumulatively covers checkpoint+1 through the current
eligible sequence, so nothing accepted is silently dropped.

Transport: triggers enqueue through the transactional outbox (#190) with the
run id as outbox/job id; the relay publishes to the queue and the worker
executes idempotently via WorkerExecution fencing (#191).

The memory/repair contents of a post-turn patch are built by the
explicit materializer (issue #217) behind ``consolidate_fn``-style
default consolidation: ``materialize_range`` compiles the range into
validated durable writes (entities, relations, facts, NPC state,
knowledge, visibility) before the criteria-driven campaign clocks
(issue #218) evaluate. Custom ``consolidate_fn`` callers bypass both
built-in phases and own their content explicitly.
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


def auto_trigger_enabled() -> bool:
    """Whether committed mutations eagerly evaluate the batch policy.

    On by default (production scheduling path); set POST_TURN_AUTO_TRIGGER=0
    to disable (e.g. focused unit tests that drive the service directly).
    """
    return os.getenv("POST_TURN_AUTO_TRIGGER", "1").lower() not in ("0", "false", "no", "off")


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
    try:
        # Savepoint-local recovery: a concurrent creator wins the insert race
        # while our read missed. The add MUST be inside the savepoint so the
        # loser is expelled on rollback instead of being re-flushed afterwards.
        # NEVER a full Session.rollback() here — this runs inside the atomic
        # mutation hook, where rolling back the outer transaction would discard
        # the authoritative event/revision (and, in the commit=False HTTP path,
        # the idempotency record) while the swallowed post-turn error lets the
        # caller continue as if committed.
        with db.begin_nested():
            db.add(rec)
            db.flush()
    except IntegrityError:
        existing = db.get(PostTurnCheckpoint, campaign_id)
        if existing is None:
            raise
        return existing
    if commit:
        db.commit()
        db.refresh(rec)
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


# Maximum worker attempts before a failed run stops suppressing coverage.
POST_TURN_MAX_ATTEMPTS = 5


def _live_coverage_to(db: Session, campaign_id: uuid.UUID) -> int:
    """Highest sequence already covered by a live (retryable) run.

    Live = pending, running, or failed still under the attempt budget. A
    dead (exhausted) run no longer counts as coverage — the next trigger
    replaces it.
    """
    to_seq = db.execute(
        select(func.max(PostTurnRun.to_sequence)).where(
            PostTurnRun.campaign_id == campaign_id,
            (PostTurnRun.status.in_(("pending", "running")))
            | ((PostTurnRun.status == "failed") & (PostTurnRun.attempts < POST_TURN_MAX_ATTEMPTS)),
        )
    ).scalar()
    return int(to_seq) if to_seq is not None else 0


def _count_relevant_since(db: Session, campaign_id: uuid.UUID, since_seq: int, upto_seq: int) -> int:
    """Relevant events with sequence in (since_seq, upto_seq]."""
    if upto_seq <= since_seq:
        return 0
    rows = db.execute(
        select(CampaignDomainEvent.event_type)
        .where(CampaignDomainEvent.campaign_id == campaign_id,
               CampaignDomainEvent.sequence > since_seq,
               CampaignDomainEvent.sequence <= upto_seq)
    ).all()
    return sum(1 for (et,) in rows if is_post_turn_relevant(et))


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

    NORMAL triggers measure the threshold against relevant events NEW since
    the latest live-run coverage (not the whole checkpoint gap), so a lagging
    checkpoint does not enqueue one overlapping run per message — the created
    run itself is still cumulative from checkpoint+1 so failed gaps stay
    authoritative. FORCE/CRITICAL bypass the batching gate.
    """
    if trigger not in (NORMAL, FORCE, CRITICAL):
        raise ValueError(f"unknown trigger {trigger!r}")
    camp = db.get(Campaign, campaign_id)
    if camp is not None and str(camp.status or "").lower() == "archived":
        # Issue #265 — dormancy freezes fictional time/clocks/NPC plans: no
        # new autonomous work is enqueued while archived. The checkpoint is
        # untouched, so post-restore triggers backfill the gap cumulatively.
        logger.info("post_turn trigger suppressed campaign=%s reason=archived", campaign_id)
        return None
    span = get_outstanding_range(db, campaign_id)
    if span["outstanding"] <= 0:
        return None
    if not should_trigger_post_turn(span["relevant"], trigger, threshold=threshold):
        return None
    from_seq, to_seq = span["from_sequence"], span["to_sequence"]

    if trigger == NORMAL:
        th = threshold if threshold is not None else get_batch_threshold()
        covered_to = max(_live_coverage_to(db, campaign_id), from_seq - 1)
        fresh = _count_relevant_since(db, campaign_id, covered_to, to_seq)
        if fresh < th:
            logger.info("post_turn suppressed campaign=%s covered_to=%s fresh=%s threshold=%s",
                        campaign_id, covered_to, fresh, th)
            return None

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
    # Race-safe insert: concurrent triggers for the same logical range share
    # one winner. A savepoint (not a full rollback) keeps the caller's outer
    # transaction intact so the loser can reuse the winning run — including
    # when called atomically from inside commit_campaign_mutation().
    from models.reliability import Outbox as _Outbox

    try:
        with db.begin_nested():
            db.add(run)
            db.flush()
            # Atomic outbox emission in the same transaction (#190 pattern).
            db.add(
                _Outbox(
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
            )
            db.flush()
    except IntegrityError:
        # Lost the race (duplicate range row or outbox id) — reuse winner.
        dup = db.execute(
            select(PostTurnRun).where(
                PostTurnRun.campaign_id == campaign_id,
                PostTurnRun.from_sequence == from_seq,
                PostTurnRun.to_sequence == to_seq,
            )
        ).scalars().first()
        if dup is not None:
            logger.info("post_turn trigger race lost campaign=%s %s-%s winner=%s", campaign_id, from_seq, to_seq, dup.id)
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
    clock_decision_service=None,
    clock_telemetry_factory=None,
) -> dict:
    """Consolidate one range and advance the checkpoint (convergent).

    Authority rule (no committed sequence is ever silently skipped): the
    EFFECTIVE range always starts at exactly checkpoint+1. A stale cumulative
    run whose stored from_sequence is behind the checkpoint is trimmed to the
    still-outstanding suffix instead of failing forever; a range reaching past
    the checkpoint backfills the gap first. Advancement is conditional on
    processed_through == effective_from-1, so a concurrent winner is detected
    instead of overwritten.
    Idempotent: a fully-processed range returns duplicate without side
    effects. On failure the checkpoint is unchanged and the run is marked
    failed with the reason; a fresh cumulative trigger converges later.
    """
    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    if to_sequence <= processed:
        logger.info("post_turn duplicate campaign=%s range=%s-%s checkpoint=%s", campaign_id, from_sequence, to_sequence, processed)
        if run_id is not None:
            run = db.get(PostTurnRun, run_id)
            if run is not None and run.status not in ("succeeded", "skipped"):
                run.status = "succeeded"
                run.completed_at = datetime.now(timezone.utc)
                if commit:
                    db.commit()
                else:
                    db.flush()
        return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": processed}

    run: PostTurnRun | None = db.get(PostTurnRun, run_id) if run_id is not None else None
    if run is None:
        # Direct call without a trigger record (tests/sweep): create the
        # durable run row keyed by job/run id when available. Add inside the
        # savepoint so a race loser is expelled, not re-flushed afterwards.
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
        try:
            with db.begin_nested():
                db.add(run)
                db.flush()
        except IntegrityError:
            # Same logical range already recorded — reuse it.
            run = db.execute(
                select(PostTurnRun).where(
                    PostTurnRun.campaign_id == campaign_id,
                    PostTurnRun.from_sequence == from_sequence,
                    PostTurnRun.to_sequence == to_sequence,
                )
            ).scalars().first()
            if run is not None and run.status in ("succeeded", "skipped"):
                return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                        "processed_through": processed}
            if run is None:
                raise RuntimeError(f"post-turn run for {campaign_id} {from_sequence}-{to_sequence} vanished after race")
    if run.status not in ("succeeded", "skipped"):
        run.status = "running"
        run.attempts = (run.attempts or 0) + 1
        db.flush()
    if commit:
        # Durable progress marker AND lock release: consolidation below can be
        # slow (model calls), and must not hold a database transaction open
        # while it runs — otherwise concurrent executors block on row/page
        # locks instead of converging via the CAS update. Authority still
        # rests solely on the prefix-conditional checkpoint UPDATE.
        db.commit()

    def _fail(exc: BaseException) -> None:
        try:
            db.rollback()
        except Exception:
            pass
        fresh = db.get(PostTurnRun, run.id) if run is not None else None
        if fresh is not None and fresh.status not in ("succeeded", "skipped"):
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

    # Convergent effective range: trim any already-processed prefix so stale
    # cumulative runs (1-5..1-8 queued while 1-4 was pending) converge instead
    # of stalling once an earlier prefix wins. The effective range ALWAYS
    # starts at checkpoint+1, so advancement can never skip a committed
    # sequence — a range reaching past the checkpoint backfills the gap first.
    effective_from = max(from_sequence, processed + 1)
    if effective_from > to_sequence:
        logger.info("post_turn duplicate campaign=%s range=%s-%s checkpoint=%s", campaign_id, from_sequence, to_sequence, processed)
        if run is not None and run.status not in ("succeeded", "skipped"):
            run.status = "succeeded"
            run.completed_at = datetime.now(timezone.utc)
            if commit:
                db.commit()
            else:
                db.flush()
        return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": processed}

    def _campaign_is_archived() -> bool:
        try:
            fresh = db.get(Campaign, campaign_id)
            return fresh is not None and str(fresh.status or "").lower() == "archived"
        except Exception as exc:
            logger.warning("post_turn archive check failed campaign=%s error=%s", campaign_id, exc)
            return False

    def _campaign_is_archived_locked() -> bool:
        """Final dormancy decision serialized with the lifecycle row.

        Archive/restore commits hold this same Campaign row lock, so once
        acquired, archive cannot commit between this check and the checkpoint
        CAS below: either archive won first (we see archived and retire) or
        we hold the lock through the CAS (archive waits, then sees the
        advanced checkpoint and backfills nothing it shouldn't). Consistent
        lock order everywhere is campaign row first, checkpoint row second.
        """
        try:
            locked = db.execute(
                select(Campaign).where(Campaign.id == campaign_id).with_for_update()
                .execution_options(populate_existing=True)
            ).scalars().first()
            return locked is not None and str(locked.status or "").lower() == "archived"
        except Exception as exc:
            logger.warning("post_turn locked archive check failed campaign=%s error=%s", campaign_id, exc)
            return False

    def _retire_skipped(reason: str) -> dict:
        logger.info(
            "post_turn skipped campaign=%s range=%s-%s reason=%s",
            campaign_id, effective_from, to_sequence, reason,
        )
        if run is not None and run.status not in ("succeeded", "skipped"):
            run.status = "skipped"
            run.failure_reason = None
            run.result = {"skipped": True, "reason": reason}
            run.completed_at = datetime.now(timezone.utc)
            if commit:
                db.commit()
            else:
                db.flush()
        return {"skipped": True, "reason": reason,
                "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": processed}

    try:
        if _campaign_is_archived():
            # Issue #265 — a run enqueued before archiving must not
            # consolidate while dormant. The checkpoint is unchanged so the
            # range is backfilled after restore; only the run row retires.
            return _retire_skipped("campaign_archived")
        events = _validate_range_contiguous(db, campaign_id, effective_from, to_sequence)
        if commit:
            # Close the read transaction: consolidation runs lock-free so a
            # concurrent executor (or admin skip) is never blocked behind it.
            # Objects stay usable (sessions use expire_on_commit=False).
            db.commit()
        if consolidate_fn is not None:
            patch = consolidate_fn(events)
        else:
            # Default consolidation: explicit materialization (issue #217)
            # followed by criteria-driven clocks (issue #218). A
            # MaterializeError propagates so the run fails and the
            # checkpoint stays put for cumulative retry.
            from app.post_turn.materialize import materialize_range

            material_campaign = db.get(Campaign, campaign_id)
            if material_campaign is None:
                raise RuntimeError(f"post-turn campaign {campaign_id} not found")
            patch = {"processed_span": [effective_from, to_sequence], "event_count": len(events)}
            patch["materialization"] = materialize_range(
                db, material_campaign, events, effective_from, to_sequence,
                decision_service=clock_decision_service,
                operation_id=operation_id,
                session_factory=clock_telemetry_factory,
            )
            # Issue #218 — criteria-driven clocks are required consolidation:
            # a clock-processing failure raises here so the run fails and the
            # checkpoint stays put for cumulative retry. Custom
            # consolidate_fn callers own their content and opt out.
            from app.world.clocks import consolidate_clocks_for_range

            patch["clocks"] = consolidate_clocks_for_range(
                db, campaign_id, effective_from, to_sequence, events,
                decision_service=clock_decision_service,
                session_factory=clock_telemetry_factory,
                operation_id=operation_id,
            )
            # Issue #220 — consistency verification is required consolidation:
            # deterministic + semantic contradictions against committed canon
            # become explicit incidents instead of silent normalization. A
            # required unresolved incident fails the run (checkpoint stays
            # put; #221 repair resolves, then a cumulative retry converges).
            # Custom consolidate_fn callers own their content and opt out.
            from app.post_turn.incidents import ConsistencyBlocked, verify_post_turn_consistency

            from app.observability.service import telemetry_factory_for

            incident_factory = (
                clock_telemetry_factory
                if clock_telemetry_factory is not None
                else telemetry_factory_for(db)
            )
            consistency = verify_post_turn_consistency(
                db, campaign_id, effective_from, to_sequence,
                decision_service=clock_decision_service,
                session_factory=clock_telemetry_factory,
                operation_id=operation_id,
                # Incidents persist on an independent transaction: the
                # ConsistencyBlocked raise below fails the run (whose
                # handler rolls back this range's materialization/clock
                # writes), while incidents stay durable for #221 repair.
                # The worker session is flush-only here; the checkpoint
                # commit below persists a successful range atomically.
                durable_session_factory=incident_factory,
                commit=False,
            )
            patch["consistency"] = {
                "complete": consistency["complete"],
                "incidents": len(consistency["incidents"]),
                "unresolved": consistency["unresolved"],
                "decision_distribution": consistency["decision_distribution"],
            }
            if not consistency["complete"]:
                ids = [i["id"] for i in consistency["incidents"]
                       if i["status"] in ("open", "deferred", "verifier_failed")]
                raise ConsistencyBlocked(
                    ids,
                    f"range {effective_from}-{to_sequence} has "
                    f"{len(ids)} unresolved consistency incident(s)",
                )
        if not isinstance(patch, dict):
            raise RuntimeError("consolidate_fn must return a dict")

        if _campaign_is_archived_locked():
            # Issue #265 — archive won during consolidation: the patch must
            # not advance the checkpoint. Retire the run; the range is
            # backfilled after restore.
            return _retire_skipped("campaign_archived_at_commit")

        # Prefix-conditional advancement on the EFFECTIVE start: exactly one
        # executor wins; a concurrent winner yields rowcount 0 (handled below).
        result = db.execute(
            update(PostTurnCheckpoint)
            .where(PostTurnCheckpoint.campaign_id == campaign_id,
                   PostTurnCheckpoint.processed_through_sequence == effective_from - 1)
            .values(processed_through_sequence=to_sequence,
                    updated_by_run_id=run.id if run is not None else None,
                    updated_at=datetime.now(timezone.utc))
        )
        if not result.rowcount:
            # Lost to a concurrent executor — re-read and classify.
            db.rollback()
            fresh_cp = db.get(PostTurnCheckpoint, campaign_id)
            fresh_processed = int(fresh_cp.processed_through_sequence or 0) if fresh_cp else processed
            if to_sequence <= fresh_processed:
                # Fully covered by the winner: retire this run row as
                # succeeded/duplicate so the sweep stops reselecting it.
                loser = db.get(PostTurnRun, run.id) if run is not None else None
                if loser is not None and loser.status not in ("succeeded", "skipped"):
                    loser.status = "succeeded"
                    loser.failure_reason = None
                    loser.result = {"duplicate": True, "covered_by_checkpoint": fresh_processed}
                    loser.completed_at = datetime.now(timezone.utc)
                    if commit:
                        db.commit()
                    else:
                        db.flush()
                return {"duplicate": True, "from_sequence": from_sequence, "to_sequence": to_sequence,
                        "processed_through": fresh_processed}
            err = RuntimeError(
                f"post-turn range {from_sequence}-{to_sequence} lost concurrent claim "
                f"(checkpoint now {fresh_processed}); retry converges"
            )
            _fail(err)
            raise err
        if run is not None:
            run.status = "succeeded"
            run.failure_reason = None
            run.result = patch
            run.completed_at = datetime.now(timezone.utc)
            db.add(run)
        if commit:
            db.commit()
        logger.info("post_turn consolidated campaign=%s %s-%s run=%s",
                    campaign_id, from_sequence, to_sequence, run.id if run else "-")
        _best_effort_running_summary(
            db, campaign_id, to_sequence,
            decision_service=clock_decision_service,
            session_factory=clock_telemetry_factory,
            commit=commit,
        )
        return {"duplicate": False, "from_sequence": from_sequence, "to_sequence": to_sequence,
                "processed_through": to_sequence, "event_count": len(events), "result": patch}
    except Exception as exc:
        # Failure reason is durable even though the checkpoint is unchanged.
        _fail(exc)
        logger.warning("post_turn failed campaign=%s %s-%s error=%s", campaign_id, from_sequence, to_sequence, exc)
        raise


def handle_post_turn_envelope(envelope, db: Session | None = None) -> dict:
    """Queue-worker handler for ``post_turn.process`` envelopes.

    Single-argument worker contract (see execute_worker_job); owns its DB
    session via SessionLocal with a ``db`` seam for tests.

    The envelope payload is a locator, not authority (per the WorkerEnvelope
    contract): execution is bound to the durable ``PostTurnRun`` loaded by
    ``envelope.job_id``. Any payload ``run_id`` must equal the job id, and
    any payload campaign/range fields must match the stored run — mismatches
    are rejected without touching the checkpoint.
    """
    payload = getattr(envelope, "payload", None) or {}
    job_id = getattr(envelope, "job_id", None)
    if job_id is None:
        raise ValueError("post_turn.process envelope is missing job_id")
    raw_run = payload.get("run_id")
    if raw_run is not None and str(raw_run) != str(job_id):
        raise ValueError(
            f"post_turn.process payload run_id {raw_run} does not match envelope job_id {job_id}"
        )

    def _run(session: Session, *, telemetry_factory=None) -> dict:
        run = session.get(PostTurnRun, job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id)))
        if run is None:
            raise ValueError(f"post-turn run {job_id} not found; refusing to execute without a durable run")
        for key, attr in (("campaign_id", "campaign_id"), ("from_sequence", "from_sequence"), ("to_sequence", "to_sequence")):
            if payload.get(key) is not None and str(payload[key]) != str(getattr(run, attr)):
                raise ValueError(
                    f"post_turn.process payload {key}={payload[key]} does not match durable run {run.id} ({attr}={getattr(run, attr)})"
                )
        return run_post_turn_range(
            session, run.campaign_id, run.from_sequence, run.to_sequence,
            run_id=run.id, operation_id=getattr(envelope, "operation_id", None),
            clock_telemetry_factory=telemetry_factory,
        )

    if db is not None:
        return _run(db)
    from database import SessionLocal

    if SessionLocal is None:
        raise RuntimeError("SessionLocal is not configured")
    with SessionLocal() as session:
        # Production worker owns its session factory: decision telemetry
        # persists on short independent transactions, fail-soft by design.
        return _run(session, telemetry_factory=SessionLocal)


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
    skip a committed sequence. Requires a non-empty reason.

    Reuses the durable run row when one already exists for the exact skipped
    span (e.g. a poison range stuck failed/pending): the row is transitioned
    to skipped with its attempt/failure history preserved in the audit
    record, so the escape hatch is never blocked by the range uniqueness
    constraint.
    """
    if not reason or not reason.strip():
        raise ValueError("an explicit audited reason is required to skip post-turn sequences")
    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    if to_sequence <= processed:
        raise ValueError(f"to_sequence {to_sequence} does not advance checkpoint {processed}")
    max_seq = get_max_sequence(db, campaign_id)
    if to_sequence > max_seq:
        raise ValueError(f"to_sequence {to_sequence} exceeds max committed sequence {max_seq}")
    audit = {"reason": reason.strip(), "actor_id": str(actor_id) if actor_id else None}
    run = db.execute(
        select(PostTurnRun).where(
            PostTurnRun.campaign_id == campaign_id,
            PostTurnRun.from_sequence == processed + 1,
            PostTurnRun.to_sequence == to_sequence,
        )
    ).scalars().first()
    if run is not None:
        audit["previous_status"] = run.status
        audit["previous_attempts"] = int(run.attempts or 0)
        audit["previous_failure_reason"] = run.failure_reason
        run.trigger = ADMIN_SKIP
        run.status = "skipped"
        run.operation_id = f"admin-skip:{actor_id}" if actor_id else "admin-skip"
        run.result = audit
        run.completed_at = datetime.now(timezone.utc)
        db.add(run)
        db.flush()
    else:
        run = PostTurnRun(
            id=uuid.uuid4(),
            campaign_id=campaign_id,
            from_sequence=processed + 1,
            to_sequence=to_sequence,
            trigger=ADMIN_SKIP,
            status="skipped",
            operation_id=f"admin-skip:{actor_id}" if actor_id else "admin-skip",
            result=audit,
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


def _best_effort_running_summary(
    db: Session,
    campaign_id: uuid.UUID,
    to_sequence: int,
    *,
    decision_service=None,
    session_factory=None,
    commit: bool = True,
) -> None:
    """Issue #219 — refresh the running summary over the processed prefix.

    Independently retryable derived work: any failure is swallowed (with a
    rollback of the summary-only transaction) so it never threatens the
    already-committed checkpoint advancement or authoritative state above.

    Verification runs on the runtime decision service by default (same
    pattern as #217 materialize / #218 clocks), so normal post-turn
    processing can actually produce a context-eligible ``current`` summary.
    A down/unconfigured verifier fails closed to ``deferred`` — retryable,
    never silently canon — rather than permanent ``pending``.
    """
    try:
        from app.decisions import DecisionService
        from app.world import summaries as _summaries

        campaign = db.get(Campaign, campaign_id)
        if campaign is None:
            return
        _summaries.consolidate_summary_for_range(
            db, campaign, 1, int(to_sequence),
            decision_service=decision_service or DecisionService(),
            session_factory=session_factory,
            operation_id=f"post-turn-summary:1-{to_sequence}",
            commit=commit,
        )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("post_turn running summary failed campaign=%s error=%s",
                       campaign_id, exc)


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
    # Issue #222 — safe-lag observability alongside the checkpoint span:
    # outstanding range size, estimated context cost, safe budget, and the
    # block state. Guarded: status reporting never raises.
    try:
        from app.post_turn.backpressure import evaluate_backpressure

        backpressure = evaluate_backpressure(db, campaign_id)
    except Exception as exc:  # noqa: BLE001 — observability must not break status
        backpressure = {"blocked": True, "reason": "estimation_failed",
                        "error": str(exc)[:300]}
    # Issue #220 — required unresolved consistency incidents alongside the
    # checkpoint span. Guarded: status reporting never raises.
    try:
        from app.post_turn.incidents import get_consistency_stats

        consistency = get_consistency_stats(db, campaign_id)
    except Exception as exc:  # noqa: BLE001 — observability must not break status
        consistency = {"unresolved": 0, "error": str(exc)[:300]}
    return {
        "campaign_id": str(campaign_id),
        "checkpoint": int(cp.processed_through_sequence or 0),
        "outstanding": span,
        "backpressure": backpressure,
        "consistency": consistency,
        "run_attempts": len(runs),
        "retry_count": retry_count,
        "last_run": last.to_dict() if last else None,
        "last_failure_reason": failed[0].failure_reason if failed else None,
        "failed_runs": len(failed),
        "queue_lag_seconds": span.get("age_seconds", 0.0),
    }


# ── Cron sweep driver ──────────────────────────────────────────────────────


def repair_missing_post_turn_runs(db: Session, *, limit: int = 20) -> list[str]:
    """Durable repair: discover campaigns whose checkpoint trails committed
    gameplay but have no live run covering the outstanding suffix, and create
    the missing cumulative run.

    This is the recovery source for eager trigger-staging failures swallowed
    by the savepoint in ``commit_campaign_mutation``: the event commits, no
    run row exists, and without this repair the range would stay outstanding
    forever when play stops. Repair preserves the configured batch policy
    (``POST_TURN_BATCH_SIZE``): intentionally sub-threshold work stays batched
    — repair only recreates what the normal policy would have triggered.
    This still converges staging failures because outstanding work only grows,
    so a range stranded at/above threshold still qualifies at repair time.
    ``maybe_trigger_post_turn`` dedupes against any live run for the range.
    """
    from sqlalchemy import func as _func

    max_seqs = dict(
        db.execute(
            select(CampaignDomainEvent.campaign_id, _func.max(CampaignDomainEvent.sequence)).group_by(
                CampaignDomainEvent.campaign_id
            )
        ).all()
    )
    if not max_seqs:
        return []
    checkpoints = dict(
        db.execute(
            select(PostTurnCheckpoint.campaign_id, PostTurnCheckpoint.processed_through_sequence).where(
                PostTurnCheckpoint.campaign_id.in_(list(max_seqs.keys()))
            )
        ).all()
    )
    live = {
        (r.campaign_id, r.to_sequence)
        for r in db.execute(
            select(PostTurnRun).where(
                PostTurnRun.campaign_id.in_(list(max_seqs.keys())),
                PostTurnRun.status.in_(("pending", "running", "failed")),
            )
        ).scalars().all()
    }
    repaired: list[str] = []
    for cid, max_seq in sorted(max_seqs.items(), key=lambda kv: str(kv[0])):
        if len(repaired) >= max(1, limit):
            break
        processed = int(checkpoints.get(cid, 0) or 0)
        if int(max_seq) <= processed:
            continue
        if (cid, int(max_seq)) in live:
            continue
        try:
            with db.begin_nested():
                run = maybe_trigger_post_turn(db, cid, trigger=NORMAL, commit=False)
                db.flush()
            if run is not None:
                repaired.append(str(run.id))
        except Exception as exc:  # noqa: BLE001 — repair must survive bad campaigns
            logger.warning("post_turn repair failed campaign=%s error=%s", cid, exc)
    if repaired:
        logger.info("post_turn repair created %s missing runs", len(repaired))
    return repaired


def _envelope_for_run(run: PostTurnRun):
    """Build the worker envelope for a durable run (locator only)."""
    from app.queue.adapter import new_envelope

    return new_envelope(
        job_id=run.id,
        job_type=POST_TURN_JOB_TYPE,
        campaign_id=run.campaign_id,
        aggregate_id=run.campaign_id,
        operation_id=run.operation_id,
        idempotency_key=str(run.id),
        trace_id=run.trace_id,
        payload={
            "run_id": str(run.id),
            "campaign_id": str(run.campaign_id),
            "from_sequence": run.from_sequence,
            "to_sequence": run.to_sequence,
            "trigger": run.trigger,
        },
    )


def run_post_turn_sweep(db: Session, *, limit: int = 5, max_attempts: int = 5, lease_seconds: int = 300) -> dict:
    """Execute outstanding post-turn runs through the worker layer.

    Production consumption path (mirrors the dm-execute cron sweep): first
    repair campaigns whose checkpoint trails gameplay with no live run
    (eager trigger-staging failures), then claim pending runs — plus failed
    runs still under the attempt budget (worker-level backoff enforced by
    ``execute_worker_job``) and ``running`` runs whose lease expired
    (executor crash between the running marker and the CAS commit; the
    prefix-conditional update makes re-execution safe) — and run each
    through the idempotent worker fence. Queue push delivery (when a
    subscriber is configured) converges on the same run rows via job_id.
    """
    from datetime import timedelta as _timedelta

    from app.worker.executor import execute_worker_job

    try:
        repaired = repair_missing_post_turn_runs(db)
        try:
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
            repaired = []
    except Exception as exc:  # noqa: BLE001 — repair must not block execution
        logger.warning("post_turn sweep repair phase failed: %s", exc)
        repaired = []
    stale_cutoff = datetime.now(timezone.utc) - _timedelta(seconds=max(60, lease_seconds))

    candidates = list(
        db.execute(
            select(PostTurnRun)
            .where(
                (PostTurnRun.status == "pending")
                | ((PostTurnRun.status == "failed") & (PostTurnRun.attempts < max_attempts))
                | ((PostTurnRun.status == "running") & (PostTurnRun.updated_at < stale_cutoff))
            )
            .order_by(PostTurnRun.created_at.asc())
            .limit(max(1, limit))
        ).scalars().all()
    )
    executed: list[str] = []
    failed: list[dict] = []
    skipped: list[str] = []
    for run in candidates:
        try:
            env = _envelope_for_run(run)
            execute_worker_job(db, env, lambda e, _db=db: handle_post_turn_envelope(e, _db),
                               max_attempts=max_attempts)
            executed.append(str(run.id))
        except Exception as exc:  # noqa: BLE001 — sweep must survive bad runs
            db.rollback()
            logger.warning("post_turn sweep run_failed run=%s error=%s", run.id, exc)
            failed.append({"run_id": str(run.id), "error": str(exc)[:300]})
    logger.info("post_turn sweep repaired=%s executed=%s failed=%s", len(repaired), len(executed), len(failed))
    return {"repaired": repaired, "executed": executed, "failed": failed, "skipped": skipped}
