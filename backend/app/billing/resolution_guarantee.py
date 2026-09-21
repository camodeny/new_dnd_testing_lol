"""Accepted-resolution guarantee with high-intensity grace — issue #254.

Deterministic, code-owned capacity policy over the #253 monetary ledger.
Never delegates billing/accounting/idempotency/authorization to a model;
this module performs no model calls and takes no narrative input.

Semantics
---------
- Capacity is checked before starting NEW AI obligations. An accepted
  player submission is guaranteed its complete owed resolution —
  clarification, requested rolls, narration, and required post-turn work —
  even when cost crosses 100% mid-resolution.
- Owed continuations (roll fulfillment, streaming, commit, post-turn
  catch-up for an accepted turn) always proceed; the gate never stops
  them. Retries/failover stay non-billable recovery work via the existing
  attempt-retry lineage (#208), so owed completion never double-charges.
- High-intensity play (rapid gameplay cadence) receives bounded grace:
  roughly 10% additional capacity while play stays rapid. Cadence and
  overage are env-configurable policy, not story inputs, and grace can
  never alter narrative/rules outcomes (this module has no narrative
  input or output at all).
- Past owed work + grace, new AI-dependent work is refused with
  :class:`CapacityPausedError` while non-AI surfaces stay usable. Resume
  is automatic from the same authoritative state when funding/BYOK
  capacity returns — no recovery wizard.
- Real-world idle during pause advances no fiction: this module performs
  zero fictional mutations (read-only gate; no revision bump, no clock
  writes — clocks additionally require committed event evidence per #218).
- The gate is read-only: duplicate evaluations create no ledger rows and
  grant no duplicate grace. Idempotency keys remain the ledger's own
  (#253); this layer adds no new writes, hence no new duplication
  surface and no migration.

Privacy: decisions consume cost/timing/entitlement aggregates only —
never narrative content.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.billing import ledger as _ledger

logger = logging.getLogger(__name__)


class CapacityPausedError(ValueError):
    """New AI-dependent work is blocked: campaign is AI-paused on capacity.

    Carries the machine-readable ``decision`` dict so transports can
    return it as a state hook (client keeps the unsent text as an editable
    local draft and retries after capacity returns). Owed continuations
    never raise this — they bypass the gate by construction.
    """

    def __init__(self, campaign_id: uuid.UUID, decision: dict):
        self.campaign_id = campaign_id
        self.decision = decision
        super().__init__(
            "AI play is paused for this campaign until capacity returns; "
            "non-AI access remains available and unsent text stays a local draft"
        )


# ── Config (env policy, not story inputs) ─────────────────────────────────────


def grace_cadence_seconds() -> float:
    """Max gap after the preceding DM response that counts as high-intensity."""
    try:
        value = float(os.getenv("RESOLUTION_GRACE_CADENCE_SECONDS", "120"))
    except (TypeError, ValueError):
        value = 120.0
    return max(1.0, value)


def grace_overage_pct() -> float:
    """Bounded extra capacity (%) while high-intensity grace applies."""
    try:
        value = float(os.getenv("RESOLUTION_GRACE_OVERAGE_PCT", "10"))
    except (TypeError, ValueError):
        value = 10.0
    return max(0.0, value)


def grace_overage_cents(funded_cents: int) -> int:
    """Deterministic overage allowance in cents for a funded threshold."""
    if funded_cents <= 0:
        return 0
    return int(funded_cents * grace_overage_pct() / 100.0)


# ── Owed-work detection (existing durable state only) ─────────────────────────


def describe_owed_work(
    db: Session, campaign_id: uuid.UUID, thread_id: str | None = None
) -> dict:
    """Describe outstanding owed AI resolution for a campaign.

    Owed work = accepted-but-unresolved submissions, turns in a
    non-terminal lifecycle state, pending player-owned rolls, and
    post-turn checkpoint lag behind the committed sequence. All sourced
    from existing durable rows; no new state.
    """
    from models.dm import DmTurn, PlayerRollRequest
    from models.post_turn import PostTurnCheckpoint
    from models.threads import PlayerSubmission

    sub_q = select(PlayerSubmission.id).where(
        PlayerSubmission.campaign_id == campaign_id,
        PlayerSubmission.resolution_status == "accepted",
    )
    if thread_id is not None:
        sub_q = sub_q.where(PlayerSubmission.thread_id == str(thread_id))
    accepted_ids = [str(v) for v in db.execute(sub_q).scalars().all()]

    turn_q = select(DmTurn.id, DmTurn.status).where(
        DmTurn.campaign_id == campaign_id,
        DmTurn.status.in_(["pending", "awaiting_roll", "streaming", "failed_visible"]),
    )
    if thread_id is not None:
        turn_q = turn_q.where(DmTurn.thread_id == str(thread_id))
    active_turns = [(str(i), str(s)) for i, s in db.execute(turn_q).all()]

    roll_q = select(func.count()).select_from(PlayerRollRequest).where(
        PlayerRollRequest.campaign_id == campaign_id,
        PlayerRollRequest.status == "pending",
    )
    pending_rolls = int(db.execute(roll_q).scalar() or 0)

    checkpoint = db.get(PostTurnCheckpoint, campaign_id)
    processed = int(checkpoint.processed_through_sequence or 0) if checkpoint else 0
    from models.campaigns import CampaignDomainEvent

    max_seq = db.execute(
        select(func.max(CampaignDomainEvent.sequence)).where(
            CampaignDomainEvent.campaign_id == campaign_id
        )
    ).scalar()
    post_turn_lag = int(max_seq or 0) - processed if max_seq else 0

    has_owed = bool(accepted_ids or active_turns or pending_rolls > 0 or post_turn_lag > 0)
    return {
        "has_owed": has_owed,
        "accepted_submission_ids": accepted_ids,
        "active_turns": [{"turn_id": tid, "status": st} for tid, st in active_turns],
        "pending_roll_count": pending_rolls,
        "post_turn_lag": max(0, post_turn_lag),
    }


def is_owed_turn(db: Session, turn_id: uuid.UUID) -> bool:
    """Whether a turn still owns owed resolution (non-terminal state)."""
    from models.dm import DmTurn

    turn = db.get(DmTurn, turn_id)
    if turn is None:
        return False
    return str(turn.status) in ("pending", "awaiting_roll", "streaming", "failed_visible")


# ── High-intensity cadence (timing only, never narrative) ─────────────────────


def _last_dm_response_at(
    db: Session, campaign_id: uuid.UUID, thread_id: str | None = None
) -> datetime | None:
    """Latest DM-visible completion (resolved/committed/streaming start).

    Timing metadata only — no narrative content is read.
    """
    from models.dm import DmTurn

    q = select(
        func.max(DmTurn.resolved_at),
        func.max(DmTurn.committed_at),
        func.max(DmTurn.streaming_started_at),
    ).where(DmTurn.campaign_id == campaign_id)
    if thread_id is not None:
        q = q.where(DmTurn.thread_id == str(thread_id))
    row = db.execute(q).first()
    if not row:
        return None
    candidates = [v for v in row if v is not None]
    if not candidates:
        return None
    latest = max(candidates)
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=timezone.utc)
    return latest


def is_high_intensity(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether recent gameplay cadence qualifies for bounded grace.

    High-intensity = the latest DM response is within the configured
    cadence window. Pure timing comparison; cannot alter narrative or
    rules outcomes (this module never touches them).
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    last_response = _last_dm_response_at(db, campaign_id, thread_id)
    if last_response is None:
        return False
    return (moment - last_response).total_seconds() <= grace_cadence_seconds()


# ── Gate ──────────────────────────────────────────────────────────────────────


def evaluate_new_work(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Decide whether a NEW AI obligation may start (read-only).

    Returns a decision dict with ``allowed`` plus the aggregate inputs
    (cost/timing/entitlement only). Failure posture: a policy-evaluation
    error errs toward allowing owed/new work and alerts via warning log —
    an already accepted obligation is never abandoned by a broken meter.
    """
    from app.observability.tracing import structured_log

    moment = now or datetime.now(timezone.utc)
    try:
        summary = _ledger.get_capacity_summary(db, campaign_id)
    except Exception as exc:
        logger.warning(
            "resolution_guarantee policy_error campaign_id=%s error=%s "
            "posture=fail_open_for_owed_work",
            campaign_id,
            exc,
        )
        structured_log(
            logger,
            logging.WARNING,
            "resolution.policy_error",
            campaign_id=str(campaign_id),
            error=str(exc)[:300],
            posture="fail_open_for_owed_work",
        )
        return {
            "allowed": True,
            "reason": "policy_error_fail_open_for_owed_work",
            "ai_paused": False,
            "grace_active": False,
            "policy_error": str(exc)[:300],
            "funded_cents": None,
            "consumed_cents": None,
            "remaining_cents": None,
            "overage_allowance_cents": 0,
        }

    funded = int(summary["funded_cents"])
    consumed = int(summary["consumed_cents"])
    remaining = int(summary["remaining_cents"])
    overage = grace_overage_cents(funded)

    base: dict[str, Any] = {
        "funded_cents": funded,
        "consumed_cents": consumed,
        "remaining_cents": remaining,
        "percent_used": summary["percent_used"],
        "overage_allowance_cents": overage,
        "grace_cadence_seconds": grace_cadence_seconds(),
        "grace_overage_pct": grace_overage_pct(),
    }

    # No funded boundary and nothing spent: capacity policy has nothing to
    # enforce yet (checkout/funding lives elsewhere). Never brick first play.
    if funded <= 0 and consumed <= 0:
        decision = {**base, "allowed": True, "reason": "unfunded_open",
                    "ai_paused": False, "grace_active": False}
        return decision

    if remaining > 0:
        return {**base, "allowed": True, "reason": "within_funded_capacity",
                "ai_paused": False, "grace_active": False}

    # Threshold crossed: bounded high-intensity grace, then pause.
    if overage > 0 and consumed <= funded + overage and is_high_intensity(
        db, campaign_id, thread_id, now=moment
    ):
        structured_log(
            logger,
            logging.INFO,
            "resolution.grace_activated",
            campaign_id=str(campaign_id),
            thread_id=str(thread_id) if thread_id else None,
            consumed_cents=consumed,
            funded_cents=funded,
            overage_allowance_cents=overage,
            cadence_seconds=grace_cadence_seconds(),
        )
        return {**base, "allowed": True, "reason": "high_intensity_grace",
                "ai_paused": False, "grace_active": True}

    structured_log(
        logger,
        logging.INFO,
        "resolution.paused",
        campaign_id=str(campaign_id),
        thread_id=str(thread_id) if thread_id else None,
        consumed_cents=consumed,
        funded_cents=funded,
    )
    return {**base, "allowed": False, "reason": "capacity_exhausted_paused",
            "ai_paused": True, "grace_active": False}


def require_new_ai_work(
    db: Session,
    campaign_id: uuid.UUID,
    thread_id: str | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Enforce the new-obligation boundary; raise when AI-paused.

    Returns the allow-decision (with ``owed`` context attached) for
    observability. Owed continuations must not call this — they proceed
    unconditionally via their lifecycle paths.
    """
    decision = evaluate_new_work(db, campaign_id, thread_id, now=now)
    if not decision["allowed"]:
        decision["owed"] = describe_owed_work(db, campaign_id, thread_id)
        raise CapacityPausedError(campaign_id, decision)
    return decision


def capacity_state_payload(
    db: Session, campaign_id: uuid.UUID, thread_id: str | None = None
) -> dict:
    """Participant-safe state hook: aggregates + pause/grace/owed flags.

    No secrets, no idempotency keys, no narrative content. Usable while
    AI-paused (non-AI surfaces stay available).
    """
    public = _ledger.public_capacity(db, campaign_id)
    decision = evaluate_new_work(db, campaign_id, thread_id)
    owed = describe_owed_work(db, campaign_id, thread_id)
    return {
        **public,
        "ai_paused": bool(decision["ai_paused"]),
        "grace_active": bool(decision.get("grace_active", False)),
        "gate_reason": decision["reason"],
        "overage_allowance_cents": decision.get("overage_allowance_cents", 0),
        "has_owed_work": owed["has_owed"],
        "owed": {
            "pending_roll_count": owed["pending_roll_count"],
            "post_turn_lag": owed["post_turn_lag"],
            "active_turn_count": len(owed["active_turns"]),
            "accepted_submission_count": len(owed["accepted_submission_ids"]),
        },
    }
