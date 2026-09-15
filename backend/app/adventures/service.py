"""Adventure lifecycle service — issue #260.

The campaign is the durable continuity boundary and stays active when an
adventure completes. Completion is a DM narrative decision committed as an
authoritative fictional mutation (``adventure.completed`` domain event tied
to source turn/event provenance); downstream closing work (recap/rewards)
is best-effort and never invalidates the committed completion.

Single canonical code path: both the HTTP API and the ``complete_adventure``
staged DM effect funnel through ``complete_adventure_inline`` inside a
``commit_campaign_mutation`` transaction.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.campaigns import Adventure, Campaign

logger = logging.getLogger(__name__)

#: Canonical completion outcome categories. Non-victory outcomes are valid
#: completions — a retreat, capture, party death/TPK, or villain victory
#: closes the arc without ending the campaign.
ADVENTURE_OUTCOMES = (
    "victory",
    "failure",
    "retreat",
    "capture",
    "death",
    "tpk",
    "villain_victory",
)

ADVENTURE_COMPLETED_EVENT = "adventure.completed"

#: Downstream closing-work job (recap/rewards). Best-effort: failure retries
#: via the worker ledger but never reopens the adventure.
ADVENTURE_CLOSING_JOB = "adventure.closing"

MAX_TITLE_LEN = 160
MAX_REASON_LEN = 2000
MAX_SUMMARY_LEN = 2000


class AdventureNotFoundError(ValueError):
    pass


class AdventureAlreadyActiveError(ValueError):
    """A new adventure cannot start while another is still active."""

    def __init__(self, campaign_id: uuid.UUID, active_id: uuid.UUID):
        self.campaign_id = campaign_id
        self.active_id = active_id
        super().__init__(
            f"Campaign {campaign_id} already has active adventure {active_id}; "
            "complete it before starting a new one"
        )


class AdventureAlreadyCompletedError(ValueError):
    """Duplicate completion of an already-completed adventure operation."""

    def __init__(self, adventure_id: uuid.UUID, outcome: str | None = None):
        self.adventure_id = adventure_id
        super().__init__(
            f"Adventure {adventure_id} is already completed"
            + (f" (outcome={outcome})" if outcome else "")
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_outcome(outcome: str) -> str:
    clean = (outcome or "").strip().lower()
    if clean not in ADVENTURE_OUTCOMES:
        raise ValueError(
            f"Invalid adventure outcome {outcome!r}; must be one of {sorted(ADVENTURE_OUTCOMES)}"
        )
    return clean


#: Staged-effect argument keys that are DM/owner-private and must never reach
#: members through serialized projections (issue #260 security). Members see
#: the outcome + player-visible summary; the completion rationale stays
#: owner-visible on the adventure record.
PRIVATE_EFFECT_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "complete_adventure": ("reason",),
}


def redact_private_effect_arguments(staged_effects: list | None) -> list:
    """Return a copy of staged effects with owner-private arguments removed."""
    redacted: list = []
    for eff in staged_effects or []:
        if not isinstance(eff, dict):
            redacted.append(eff)
            continue
        private = PRIVATE_EFFECT_ARGUMENTS.get(eff.get("effect_type"))
        args = eff.get("arguments")
        if not private or not isinstance(args, dict):
            redacted.append(eff)
            continue
        eff = dict(eff)
        eff["arguments"] = {k: v for k, v in args.items() if k not in private}
        redacted.append(eff)
    return redacted


def redact_private_contract_snapshot(snapshot: dict | None) -> dict | None:
    """Project a contract snapshot to its audience-safe public form.

    Uses the contract's own allowlisted ``public_projection`` so internal
    lanes (top-level ``reason``, staged effects, evidence, provenance,
    private beat context) never reach members. Unparseable snapshots are
    omitted entirely (fail closed) rather than leaked partially.
    """
    if not isinstance(snapshot, dict):
        return snapshot
    try:
        from app.dm.contract import normalize_contract, public_projection

        return public_projection(normalize_contract(snapshot))
    except Exception:
        logger.warning("adventure redaction dropped unparseable contract snapshot")
        return None


def get_adventure(db: Session, adventure_id: uuid.UUID) -> Adventure | None:
    return db.get(Adventure, adventure_id)


def get_current_adventure(db: Session, campaign_id: uuid.UUID) -> Adventure | None:
    """The active (uncompleted) adventure for a campaign, if any."""
    return (
        db.execute(
            select(Adventure)
            .where(Adventure.campaign_id == campaign_id, Adventure.status == "active")
            .order_by(Adventure.started_at.desc(), Adventure.created_at.desc())
        )
        .scalars()
        .first()
    )


def list_adventures(db: Session, campaign_id: uuid.UUID) -> list[Adventure]:
    return list(
        db.execute(
            select(Adventure)
            .where(Adventure.campaign_id == campaign_id)
            .order_by(Adventure.started_at.asc(), Adventure.created_at.asc())
        )
        .scalars()
        .all()
    )


def find_by_operation(db: Session, campaign_id: uuid.UUID, operation_id: str) -> Adventure | None:
    if not operation_id:
        return None
    return (
        db.execute(
            select(Adventure).where(
                Adventure.campaign_id == campaign_id,
                Adventure.operation_id == operation_id,
            )
        )
        .scalars()
        .first()
    )


def start_adventure(
    db: Session,
    campaign_id: uuid.UUID,
    title: str,
    *,
    commit: bool = True,
    adventure_metadata: dict | None = None,
) -> Adventure:
    """Open a new adventure arc. Fails if one is already active.

    The campaign row is locked for the check-then-insert so concurrent
    starts serialize; the partial unique index
    ``uq_adventures_one_active_per_campaign`` is the final backstop and a
    unique violation is mapped to :class:`AdventureAlreadyActiveError`.
    """
    from sqlalchemy.exc import IntegrityError

    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("Adventure title is required")
    if len(clean_title) > MAX_TITLE_LEN:
        raise ValueError(f"Adventure title must be at most {MAX_TITLE_LEN} characters")
    try:
        campaign = db.execute(
            select(Campaign).where(Campaign.id == campaign_id).with_for_update()
        ).scalars().first()
    except Exception:
        campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise AdventureNotFoundError(f"Campaign {campaign_id} not found")
    existing = get_current_adventure(db, campaign_id)
    if existing is not None:
        raise AdventureAlreadyActiveError(campaign_id, existing.id)
    adventure = Adventure(
        id=uuid.uuid4(),
        campaign_id=campaign_id,
        title=clean_title,
        status="active",
        adventure_metadata=adventure_metadata,
    )
    db.add(adventure)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        logger.warning(
            "adventure concurrent start rejected campaign_id=%s error=%s",
            campaign_id, exc,
        )
        # Re-read the winner for an actionable error.
        winner = get_current_adventure(db, campaign_id)
        raise AdventureAlreadyActiveError(
            campaign_id, winner.id if winner is not None else adventure.id
        ) from exc
    if commit:
        db.commit()
        db.refresh(adventure)
    logger.info(
        "adventure started campaign_id=%s adventure_id=%s title=%s",
        campaign_id, adventure.id, clean_title,
    )
    return adventure


def complete_adventure_inline(
    db: Session,
    campaign: Campaign,
    adventure: Adventure,
    *,
    outcome: str,
    reason: str | None = None,
    public_summary: str | None = None,
    source_turn_id: uuid.UUID | None = None,
    operation_id: str | None = None,
) -> Adventure:
    """Close an adventure inside the caller's revision transaction.

    Must be called within ``commit_campaign_mutation``'s ``mutate`` callback
    (or any equivalent transactional context): any exception rolls back the
    whole commit, so a failed completion leaves the adventure open rather
    than half-complete.

    Raises:
        AdventureAlreadyCompletedError: if the adventure is already completed
            (duplicate/retried completion protection).
    """
    if adventure.status == "completed":
        raise AdventureAlreadyCompletedError(adventure.id, adventure.outcome)
    clean_outcome = validate_outcome(outcome)
    clean_reason = (reason or "").strip() or None
    if clean_reason is not None and len(clean_reason) > MAX_REASON_LEN:
        raise ValueError(f"Adventure completion reason must be at most {MAX_REASON_LEN} characters")
    clean_summary = (public_summary or "").strip() or None
    if clean_summary is not None and len(clean_summary) > MAX_SUMMARY_LEN:
        raise ValueError(f"Adventure public summary must be at most {MAX_SUMMARY_LEN} characters")
    if str(adventure.campaign_id) != str(campaign.id):
        raise ValueError(
            f"Adventure {adventure.id} does not belong to campaign {campaign.id}"
        )
    adventure.status = "completed"
    adventure.outcome = clean_outcome
    adventure.reason = clean_reason
    adventure.public_summary = clean_summary
    if source_turn_id is not None:
        adventure.source_turn_id = source_turn_id
    if operation_id:
        adventure.operation_id = operation_id
    adventure.closing_status = "pending"
    adventure.completed_at = _now()
    db.flush()
    return adventure


def complete_adventure(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    outcome: str,
    reason: str | None = None,
    public_summary: str | None = None,
    adventure_id: uuid.UUID | None = None,
    source_turn_id: uuid.UUID | None = None,
    operation_id: str | None = None,
    actor_id: uuid.UUID | None = None,
    expected_revision: int | None = None,
    commit: bool = True,
) -> tuple[Adventure, Any]:
    """Declare the current adventure complete as an authoritative mutation.

    - Idempotent on ``operation_id``: a retried completion returns the
      original adventure + event instead of creating duplicates.
    - Emits the ``adventure.completed`` domain event with turn/event
      provenance and enqueues ``adventure.closing`` downstream work.
    - The campaign status is untouched — it stays active/continuable and
      later adventures can be created in the same world.

    Returns:
        (adventure, event). On duplicate operation replay, returns the
        existing (adventure, event) with ``duplicate=True`` in the caller
        response (the event payload itself is unchanged).
    """
    from app.campaigns.events import commit_campaign_mutation

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise AdventureNotFoundError(f"Campaign {campaign_id} not found")

    # Idempotency first: a retried operation must not create duplicate
    # adventure records/events.
    if operation_id:
        prior = find_by_operation(db, campaign_id, operation_id)
        if prior is not None and prior.status == "completed":
            from models.campaigns import CampaignDomainEvent

            event = (
                db.execute(
                    select(CampaignDomainEvent).where(
                        CampaignDomainEvent.campaign_id == campaign_id,
                        CampaignDomainEvent.operation_id == operation_id,
                    )
                )
                .scalars()
                .first()
            )
            logger.info(
                "adventure duplicate_completion_hit campaign_id=%s adventure_id=%s op=%s",
                campaign_id, prior.id, operation_id,
            )
            return prior, event

    if adventure_id is not None:
        adventure = db.get(Adventure, adventure_id)
        if adventure is None or str(adventure.campaign_id) != str(campaign_id):
            raise AdventureNotFoundError(f"Adventure {adventure_id} not found in campaign {campaign_id}")
    else:
        adventure = get_current_adventure(db, campaign_id)
        if adventure is None:
            raise AdventureNotFoundError(f"Campaign {campaign_id} has no active adventure to complete")

    if adventure.status == "completed":
        raise AdventureAlreadyCompletedError(adventure.id, adventure.outcome)

    expected = expected_revision if expected_revision is not None else int(campaign.revision or 0)
    completed: dict[str, Adventure] = {}

    def _mutate(locked: Campaign):
        adv = db.get(Adventure, adventure.id)
        if adv is None:
            raise AdventureNotFoundError(f"Adventure {adventure.id} not found")
        complete_adventure_inline(
            db, locked, adv,
            outcome=outcome, reason=reason, public_summary=public_summary,
            source_turn_id=source_turn_id, operation_id=operation_id,
        )
        completed["adventure"] = adv

    def _payload() -> dict:
        adv = completed["adventure"]
        # Player-readable lifecycle data only: the DM's completion reason
        # stays on the owner-visible adventure row, never in the public
        # domain-event feed (issue #260 security).
        return {
            "adventure_id": str(adv.id),
            "title": adv.title,
            "outcome": adv.outcome,
            "public_summary": adv.public_summary,
            "source_turn_id": str(adv.source_turn_id) if adv.source_turn_id else None,
            "source_event_id": str(adv.source_event_id) if adv.source_event_id else None,
            "campaign_status": campaign.status,
        }

    def _provenance() -> dict:
        return {
            "source_turn_id": str(source_turn_id) if source_turn_id else None,
            "declared_by": "dm",
        }

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision=int(expected),
        event_type=ADVENTURE_COMPLETED_EVENT,
        operation_id=operation_id,
        actor_id=actor_id,
        targets={"adventure_id": str(adventure.id)},
        provenance=_provenance(),
        mutate=_mutate,
        commit=False,
        payload_builder=_payload,
        outbox_event_type=ADVENTURE_CLOSING_JOB,
        outbox_payload={
            "adventure_id": str(adventure.id),
            "campaign_id": str(campaign_id),
            "outcome": validate_outcome(outcome),
            "operation_id": operation_id,
        },
        outbox_operation_id=operation_id,
    )

    # Link the authoritative event back onto the adventure for provenance.
    adv = completed["adventure"]
    adv.source_event_id = event.id
    db.flush()
    if commit:
        db.commit()
        db.refresh(adv)
        db.refresh(event)
        db.refresh(campaign_after)

    logger.info(
        "adventure completed campaign_id=%s adventure_id=%s outcome=%s revision=%s event_id=%s campaign_status=%s op=%s",
        campaign_id, adv.id, adv.outcome, campaign_after.revision, event.id,
        campaign_after.status, operation_id or "-",
    )
    return adv, event


# ── Downstream closing work (best-effort) ────────────────────────────────────


def handle_adventure_closing(envelope, db: Session | None = None) -> dict:
    """Worker handler for ``adventure.closing`` — recap/reward follow-ups.

    Best-effort by design: a failure here is retried via the worker ledger
    but never invalidates the already-committed narrative completion.
    """
    from app.worker.executor import RetriableError
    from database import SessionLocal

    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True
    try:
        payload = envelope.payload or {}
        adventure_id = payload.get("adventure_id")
        if not adventure_id:
            raise ValueError("adventure.closing envelope missing adventure_id")
        adventure = db.get(Adventure, uuid.UUID(str(adventure_id)))
        if adventure is None:
            logger.warning("adventure closing unknown adventure_id=%s", adventure_id)
            return {"ok": False, "reason": "unknown_adventure"}
        if adventure.status != "completed":
            # Completion rolled back or not yet committed — retry later.
            raise RetriableError(f"Adventure {adventure_id} is not completed yet")
        if adventure.closing_status == "succeeded":
            return {"ok": True, "duplicate": True, "adventure_id": str(adventure.id)}
        adventure.closing_attempts = int(adventure.closing_attempts or 0) + 1
        try:
            _run_closing_followups(db, adventure)
        except RetriableError:
            raise
        except Exception as exc:
            adventure.closing_status = "failed"
            adventure.closing_error = str(exc)[:1000]
            db.flush()
            db.commit()
            logger.warning(
                "adventure closing failed adventure_id=%s attempt=%s error=%s",
                adventure.id, adventure.closing_attempts, exc,
            )
            raise RetriableError(str(exc)) from exc
        adventure.closing_status = "succeeded"
        adventure.closing_error = None
        db.flush()
        db.commit()
        logger.info(
            "adventure closing succeeded adventure_id=%s outcome=%s",
            adventure.id, adventure.outcome,
        )
        return {"ok": True, "adventure_id": str(adventure.id), "outcome": adventure.outcome}
    finally:
        if own_session:
            db.close()


def _run_closing_followups(db: Session, adventure: Adventure) -> None:
    """Placeholder closing follow-ups (recap/reward derivation).

    Computes a duration + outcome record into adventure metadata. Derived
    bookkeeping only — intentionally side-effect free beyond the adventure
    row so failures stay contained and retryable.
    """
    meta = dict(adventure.adventure_metadata or {})
    closing = dict(meta.get("closing") or {})
    started = adventure.started_at
    completed = adventure.completed_at or _now()
    try:
        duration_s = max(0, int((completed - started).total_seconds())) if started else 0
    except Exception:
        duration_s = 0
    closing.update({
        "outcome": adventure.outcome,
        "duration_seconds": duration_s,
        "source_turn_id": str(adventure.source_turn_id) if adventure.source_turn_id else None,
        "source_event_id": str(adventure.source_event_id) if adventure.source_event_id else None,
    })
    meta["closing"] = closing
    adventure.adventure_metadata = meta
    db.flush()


def register_adventure_worker() -> None:
    from app.queue.consumer import WORKER_HANDLERS

    WORKER_HANDLERS[ADVENTURE_CLOSING_JOB] = handle_adventure_closing


def run_adventure_closing_sweep(db: Session, *, limit: int = 5, max_attempts: int = 5) -> dict:
    """Drive pending adventure closing work through the idempotent worker fence.

    Production consumption path for ``adventure.closing`` (mirrors the
    post-turn sweep): consumes the durable outbox rows directly, translating
    each through ``envelope_for_outbox`` — the exact translation the relay
    uses — so queue delivery and this sweep converge on one
    ``WorkerExecution`` per outbox row. A failed sweep never invalidates the
    already-committed narrative completion.
    """
    from datetime import datetime as _datetime
    from datetime import timezone as _timezone

    from sqlalchemy import or_ as _or_

    from app.outbox.service import ack_published, envelope_for_outbox, mark_failed
    from app.worker.executor import TerminalError, execute_worker_job
    from models.reliability import Outbox

    now = _datetime.now(_timezone.utc)
    candidates = list(
        db.execute(
            select(Outbox)
            .where(
                Outbox.event_type == ADVENTURE_CLOSING_JOB,
                _or_(Outbox.status == "pending", Outbox.status == "failed"),
                _or_(Outbox.next_attempt_at == None, Outbox.next_attempt_at <= now),  # noqa: E711
            )
            .order_by(Outbox.created_at.asc())
            .limit(max(1, limit))
        )
        .scalars()
        .all()
    )
    executed: list[str] = []
    failed: list[dict] = []
    for row in candidates:
        try:
            env = envelope_for_outbox(row)
            execute_worker_job(
                db, env, lambda e, _db=db: handle_adventure_closing(e, _db),
                max_attempts=max_attempts,
            )
            ack_published(db, row.id)
            executed.append(str(row.id))
        except TerminalError as exc:
            # The worker ledger durably owns the terminal outcome
            # (dead_letter): retire the transport row so a poisoned job can
            # never be reselected or starve newer closing work. The
            # adventure itself stays completed; only best-effort closing
            # remains failed and inspectable.
            try:
                db.rollback()
            except Exception:
                pass
            ack_published(db, row.id)
            logger.warning(
                "adventure closing sweep retired terminal outbox_id=%s error=%s",
                row.id, exc,
            )
            failed.append({"outbox_id": str(row.id), "error": str(exc)[:300], "terminal": True})
        except Exception as exc:  # noqa: BLE001 — sweep must survive bad rows
            try:
                db.rollback()
            except Exception:
                pass
            try:
                mark_failed(db, row.id, str(exc)[:500])
            except Exception:
                pass
            logger.warning(
                "adventure closing sweep failed outbox_id=%s error=%s",
                row.id, exc,
            )
            failed.append({"outbox_id": str(row.id), "error": str(exc)[:300]})
    logger.info(
        "adventure closing sweep executed=%s failed=%s", len(executed), len(failed)
    )
    return {"executed": executed, "failed": failed}


register_adventure_worker()
