"""Campaign revision ordering + immutable domain events — issue #188.

Provides the single application service for committing authoritative fictional
mutations against an expected campaign revision. Guarantees:

- Expected-revision optimistic concurrency (409 on stale).
- Revision increments exactly once per successful fictional mutation.
- Domain event gets stable monotonic campaign sequence == resulting revision.
- Whole operation is transactional — rollback leaves revision + events unchanged.
- Non-fictional derived updates bypass revision intentionally.

Observability: logs campaign_id, prior/new revision, operation_id, conflict.

Usage:
    from app.campaigns.events import commit_campaign_mutation, RevisionConflictError

    def mutate(campaign):
        campaign.name = "New Name"

    campaign, event = commit_campaign_mutation(
        db, campaign_id, expected_revision=3,
        event_type="campaign.renamed", payload={"name": "New Name"},
        operation_id="op-123", actor_id=profile.id,
        mutate=mutate,
    )

    # Non-fictional: don't bump revision
    update_campaign_derived(db, campaign_id, description="index only")
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from models.campaigns import Campaign
from models.campaigns import CampaignDomainEvent

logger = logging.getLogger(__name__)


class RevisionConflictError(Exception):
    """Raised when expected_revision does not match current campaign revision.

    Maps to HTTP 409 Conflict at the API boundary. Caller should re-read the
    campaign and recompute the operation — not retry with same expected revision.
    """

    def __init__(
        self,
        campaign_id: uuid.UUID,
        expected: int,
        actual: int,
        operation_id: str | None = None,
    ):
        self.campaign_id = campaign_id
        self.expected_revision = expected
        self.actual_revision = actual
        self.operation_id = operation_id
        super().__init__(
            f"Revision conflict for campaign {campaign_id}: expected {expected}, actual {actual}"
            + (f" (op {operation_id})" if operation_id else "")
        )


# ── Core service ────────────────────────────────────────────────────────────


def commit_campaign_mutation(
    db: Session,
    campaign_id: uuid.UUID,
    expected_revision: int,
    *,
    event_type: str,
    payload: dict | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    targets: dict | list | None = None,
    visibility: str = "public",
    provenance: dict | None = None,
    mutate: Optional[Callable[[Campaign], None]] = None,
    commit: bool = True,
    payload_builder: Optional[Callable[[], dict]] = None,
    targets_builder: Optional[Callable[[], dict | list]] = None,
    visibility_builder: Optional[Callable[[], str]] = None,
    outbox_event_type: str | None = None,
    outbox_payload: dict | None = None,
    outbox_operation_id: str | None = None,
) -> tuple[Campaign, CampaignDomainEvent]:
    """Commit an authoritative fictional mutation transactionally.

    Args:
        db: SQLAlchemy session (caller manages lifecycle; this adds + flushes).
        campaign_id: Campaign to mutate.
        expected_revision: Revision the caller observed before computing this mutation.
        event_type: Domain event type string (e.g. "campaign.renamed").
        payload: Event payload (domain data for provenance/history).
        operation_id: Source operation id for idempotency/observability.
        actor_id: Profile id of the actor.
        targets: Affected entity ids / targets.
        visibility: Visibility hook (public/private/dm_only etc.).
        provenance: Free-form provenance JSON.
        mutate: Optional callable receiving the locked Campaign to apply state changes.
                Executed within the same transaction after revision bump.
        commit: Whether to commit the transaction (default True). When False,
                caller controls commit/rollback (useful for composing).
        payload_builder: Optional zero-arg callable invoked AFTER mutate succeeds
                to build the event payload from mutation results (e.g. ids
                allocated inside mutate). Takes precedence over payload.
        targets_builder: Optional zero-arg callable invoked AFTER mutate succeeds
                to build event targets. Takes precedence over targets.
        visibility_builder: Optional zero-arg callable invoked AFTER mutate
                succeeds to build event visibility from mutation results (e.g.
                the resulting record's disclosure level). Takes precedence
                over visibility.

    Returns:
        (campaign, event) after commit (campaign.revision == event.sequence).

    Raises:
        RevisionConflictError: if expected_revision != current revision.
        ValueError: if campaign not found.
    """
    if not event_type or not event_type.strip():
        raise ValueError("event_type is required")
    if expected_revision is None or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")

    # Lock the campaign row. SQLite ignores FOR UPDATE in unit tests; Postgres
    # serializes competing writers before the conditional revision update below.
    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()

    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found")

    prior = int(campaign.revision) if campaign.revision is not None else 0
    if prior != int(expected_revision):
        logger.warning(
            "campaign mutation conflict campaign_id=%s op=%s expected=%s actual=%s event_type=%s",
            campaign_id,
            operation_id or "-",
            expected_revision,
            prior,
            event_type,
        )
        raise RevisionConflictError(campaign_id, int(expected_revision), prior, operation_id)

    new_revision = prior + 1

    # Apply caller mutation BEFORE event insert so rollback covers both.
    # Note: campaign object already has prior revision; we bump after mutate.
    if mutate is not None:
        try:
            mutate(campaign)
        except Exception:
            # Ensure rollback hygiene if caller does flushes that partially dirty session
            db.rollback()
            logger.warning(
                "campaign mutation aborted by mutate() campaign_id=%s op=%s prior=%s reason=mutate_exception",
                campaign_id,
                operation_id or "-",
                prior,
            )
            raise

    # Atomically bump revision — conditional update guards against race where
    # another txn committed between our SELECT and now.
    # If 0 rows affected, another committer won the race.
    result = db.execute(
        update(Campaign)
        .where(Campaign.id == campaign_id, Campaign.revision == expected_revision)
        .values(revision=new_revision)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # Another writer won; refresh to get actual revision for error
        db.rollback()
        # re-read actual
        fresh = db.execute(select(Campaign).where(Campaign.id == campaign_id)).scalars().first()
        actual = int(fresh.revision) if fresh and fresh.revision is not None else -1
        logger.warning(
            "campaign mutation race conflict campaign_id=%s op=%s expected=%s actual=%s event_type=%s",
            campaign_id,
            operation_id or "-",
            expected_revision,
            actual,
            event_type,
        )
        raise RevisionConflictError(campaign_id, int(expected_revision), actual, operation_id)

    # Keep in-memory campaign in sync (we already have it locked)
    campaign.revision = new_revision
    # Expire to ensure updated_at trigger etc; but not required.
    db.flush()

    # Resolve payload/targets AFTER mutate so events capture mutation results
    # (e.g. entity ids allocated inside mutate). Builders run here — after the
    # mutation succeeded but before the event insert — so the immutable event
    # history reflects what actually happened.
    resolved_payload = payload_builder() if payload_builder is not None else payload
    resolved_targets = targets_builder() if targets_builder is not None else targets
    resolved_visibility = (
        visibility_builder() if visibility_builder is not None else visibility
    )

    # 2) Insert immutable domain event with sequence == new_revision
    from app.observability.tracing import current_trace_id

    event = CampaignDomainEvent(
        id=uuid.uuid4(),
        campaign_id=campaign_id,
        sequence=new_revision,
        event_type=event_type.strip(),
        operation_id=operation_id,
        trace_id=current_trace_id(),
        actor_id=actor_id,
        targets=resolved_targets,
        payload=resolved_payload,
        visibility=resolved_visibility or "public",
        provenance=provenance,
    )
    db.add(event)

    try:
        db.flush()
    except Exception as e:
        # Unique violation means someone assigned same sequence (should be impossible
        # due to revision guard, but be defensive).
        db.rollback()
        logger.warning(
            "campaign mutation event insert failed campaign_id=%s op=%s prior=%s new=%s error=%s",
            campaign_id,
            operation_id or "-",
            prior,
            new_revision,
            e,
        )
        raise

    # 3) Optionally enqueue outbox atomically in same transaction (issue #190)
    if outbox_event_type:
        # lazy import to avoid circular
        from models.reliability import Outbox as _Outbox
        ob = _Outbox(
            id=uuid.uuid4(),
            aggregate_type="campaign",
            aggregate_id=campaign_id,
            campaign_id=campaign_id,
            event_type=outbox_event_type,
            operation_id=outbox_operation_id or operation_id,
            trace_id=current_trace_id(),
            payload=outbox_payload if outbox_payload is not None else resolved_payload,
            status="pending",
            attempts=0,
        )
        db.add(ob)
        db.flush()

    if commit:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
        # refresh to get server defaults (created_at etc)
        db.refresh(campaign)
        db.refresh(event)

    logger.info(
        "campaign mutation %s campaign_id=%s prior=%s new=%s op=%s event_type=%s event_id=%s",
        "committed" if commit else "staged",
        campaign_id,
        prior,
        new_revision,
        operation_id or "-",
        event_type,
        event.id,
    )

    return campaign, event


def update_campaign_derived(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    commit: bool = True,
    **fields,
) -> Campaign:
    """Update non-fictional derived/index fields WITHOUT bumping revision.

    This is the correct path for metadata/derived updates that must not be
    considered authoritative fictional mutations (per AC: non-fictional derived
    updates must NOT advance revision).

    Example: search index, denormalized counters, cached projections.

    Whitelisted fields are applied directly to the Campaign row. Unknown keys
    are ignored to avoid accidental fictional field bumps.

    Args:
        db: Session
        campaign_id: Campaign id
        fields: keyword updates (e.g. description via derived path? Usually very
                limited. For flexibility, any Campaign column except revision is accepted
                but callers should be deliberate.)

    Returns:
        Updated Campaign (revision unchanged).
    """
    campaign = db.execute(select(Campaign).where(Campaign.id == campaign_id)).scalars().first()
    if campaign is None:
        raise ValueError(f"Campaign {campaign_id} not found")

    prior_revision = int(campaign.revision) if campaign.revision is not None else 0

    # Campaign currently has one derived metadata field. Keep this explicit so
    # fictional fields cannot accidentally bypass revision ordering.
    allowed = {"updated_at"}
    applied = {}
    for k, v in fields.items():
        if k in allowed:
            setattr(campaign, k, v)
            applied[k] = v

    if not applied:
        return campaign

    db.flush()
    if commit:
        db.commit()
        db.refresh(campaign)

    logger.info(
        "campaign derived update campaign_id=%s revision=%s fields=%s (no revision bump)",
        campaign_id,
        prior_revision,
        list(applied.keys()),
    )
    # Ensure revision truly unchanged
    assert int(campaign.revision) == prior_revision, "derived update must not bump revision"
    return campaign


def list_campaign_events(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CampaignDomainEvent]:
    """Ordered history for a campaign (sequence asc)."""
    query = select(CampaignDomainEvent).where(CampaignDomainEvent.campaign_id == campaign_id)
    if viewer_id is not None:
        query = query.where(
            or_(
                CampaignDomainEvent.visibility == "public",
                CampaignDomainEvent.actor_id == viewer_id,
            )
        )
    return list(
        db.execute(
            query
            .order_by(CampaignDomainEvent.sequence.asc())
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
