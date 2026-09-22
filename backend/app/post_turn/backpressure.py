"""Safe lag / backpressure for post-turn processing — issue #222.

Forward play may continue while post-turn trails the committed event
sequence, but only while the forward DM can still reason reliably from
the accumulated unprocessed committed history. Safety is based on the
actual approved context budget — never on an arbitrary turn count.

Deterministic code owns every bound here (ordering, checkpoint reads,
cost estimation, the block decision, idempotent catch-up triggers).
Models only judge semantic content inside already-authorized ranges
(#217/#218) and never authorize progression.

Rules:
- While the estimated cost of unprocessed committed history fits the
  safe forward-DM context budget, gap-fill records expose it directly
  in forward-DM context assembly (see ``app.dm.context``) so narration
  never claims an unaware state.
- When the backlog exceeds the safe budget, new AI progression pauses
  (``BackpressureBlocked``) BEFORE context becomes unreliable, a
  critical catch-up trigger fires, and accepted player input stays
  durable (coordination/acceptance is untouched — only execution pauses).
- Estimation failure blocks conservatively: never silently omit history.
- Client-facing state is low-key (ready/processing) with no queue,
  provider, model, or infrastructure detail.
"""

from __future__ import annotations

import logging
import math
import os
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from models.campaigns import CampaignDomainEvent

logger = logging.getLogger(__name__)

# ── Safe context budget (single canonical knob) ──────────────────────────
# One budget for the approved forward-DM role: the forward model must be
# able to hold ALL unprocessed committed history plus the required
# authoritative lanes. Defaults sit below the assembler total budget
# (64_000 bytes / 16_000 tokens in app.dm.context) so the gate trips
# before assembly-level budget pressure can silently drop required
# history. Per-model tuning collapses to this one env pair (pre-alpha:
# no legacy aliases).

DEFAULT_SAFE_CONTEXT_BYTES = 48_000
DEFAULT_SAFE_CONTEXT_TOKENS = 12_000

# Conservative per-record envelope margin (sources, authorization, ids)
# added on top of each event's measured payload size so the estimate
# always meets or exceeds the real assembled record cost.
RECORD_ENVELOPE_MARGIN_BYTES = 512


class BackpressureBlocked(Exception):
    """New AI progression must pause until post-turn catches up."""

    code = "post_turn_backpressure"

    def __init__(self, status: dict[str, Any]):
        self.status = status
        super().__init__(
            "Post-turn is behind beyond the safe context budget; "
            f"AI progression paused (outstanding={status.get('outstanding')}, "
            f"estimated_bytes={status.get('estimated_bytes')})."
        )


def get_safe_context_budget_bytes() -> int:
    try:
        return max(1024, int(os.getenv("FORWARD_DM_SAFE_CONTEXT_BYTES", str(DEFAULT_SAFE_CONTEXT_BYTES))))
    except (TypeError, ValueError):
        return DEFAULT_SAFE_CONTEXT_BYTES


def get_safe_context_budget_tokens() -> int:
    try:
        return max(256, int(os.getenv("FORWARD_DM_SAFE_CONTEXT_TOKENS", str(DEFAULT_SAFE_CONTEXT_TOKENS))))
    except (TypeError, ValueError):
        return DEFAULT_SAFE_CONTEXT_TOKENS


def get_safe_context_budget() -> tuple[int, int]:
    """(bytes, tokens) effective safe budget for the forward-DM role."""
    return get_safe_context_budget_bytes(), get_safe_context_budget_tokens()


def estimate_unprocessed_cost(
    db: Session, campaign_id: uuid.UUID, *, upto_sequence: int | None = None
) -> dict[str, int]:
    """Deterministic estimated context cost of unprocessed committed history.

    Sizes the exact records context assembly must keep: every unprocessed
    event is built through ``app.dm.context._history_record`` (required,
    with the post-turn marker) and measured with the same serialized-record
    ``_size`` the packet budget enforces — so unbounded fields such as
    ``event.provenance`` (carried in ``SourceRef.provenance``) plus
    ids/operation/trace metadata are all accounted for. Events assembly
    itself omits (scopeless private history) contribute nothing here either.
    Raises on any failure — callers must treat that as blocked, never as
    zero (fail-safe: never silently omit history).
    """
    from app.dm.context import _history_record
    from app.dm.context import _size as _record_size
    from app.post_turn.service import get_checkpoint, get_max_sequence

    cp = get_checkpoint(db, campaign_id, commit=False)
    processed = int(cp.processed_through_sequence or 0)
    max_seq = int(upto_sequence) if upto_sequence is not None else get_max_sequence(db, campaign_id)
    if max_seq <= processed:
        return {"estimated_bytes": 0, "estimated_tokens": 0, "event_count": 0}
    events = db.scalars(
        select(CampaignDomainEvent)
        .where(
            CampaignDomainEvent.campaign_id == campaign_id,
            CampaignDomainEvent.sequence > processed,
            CampaignDomainEvent.sequence <= max_seq,
        )
        .order_by(CampaignDomainEvent.sequence.asc())
    ).all()
    total = 0
    for event in events:
        record = _history_record(
            campaign_id, event,
            required=True, priority=95,
            post_turn_processed_through=processed,
        )
        if record is None:
            continue
        total += _record_size(record.model_dump(mode="json"))
    # Lag-summary record overhead carried alongside the gap-fill records.
    total += RECORD_ENVELOPE_MARGIN_BYTES
    return {
        "estimated_bytes": total,
        "estimated_tokens": math.ceil(total / 4),
        "event_count": len(events),
    }


def evaluate_backpressure(db: Session, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Evaluate the safe-lag gate (fail conservative).

    Returns a status dict with the outstanding range, estimated cost, safe
    budget, ``blocked`` flag, reason, and observability fields (range size,
    estimated cost, lag age, suggested catch-up trigger). Estimation or
    checkpoint failure yields ``blocked=True`` with reason
    ``estimation_failed`` rather than silent omission.
    """
    from app.post_turn.service import get_outstanding_range

    safe_bytes, safe_tokens = get_safe_context_budget()
    try:
        span = get_outstanding_range(db, campaign_id)
        cost = estimate_unprocessed_cost(db, campaign_id)
    except Exception as exc:
        logger.warning("backpressure estimation failed campaign=%s error=%s", campaign_id, exc)
        return {
            "campaign_id": str(campaign_id),
            "blocked": True,
            "reason": "estimation_failed",
            "error": f"{type(exc).__name__}: {exc}"[:300],
            "safe_budget_bytes": safe_bytes,
            "safe_budget_tokens": safe_tokens,
            "estimated_bytes": None,
            "estimated_tokens": None,
            "outstanding": None,
            "suggested_trigger": "critical",
        }
    over_bytes = cost["estimated_bytes"] > safe_bytes
    over_tokens = cost["estimated_tokens"] > safe_tokens
    blocked = bool(span["outstanding"] > 0 and (over_bytes or over_tokens))
    return {
        "campaign_id": str(campaign_id),
        "blocked": blocked,
        "reason": "over_safe_budget" if blocked else "within_budget",
        "safe_budget_bytes": safe_bytes,
        "safe_budget_tokens": safe_tokens,
        "estimated_bytes": cost["estimated_bytes"],
        "estimated_tokens": cost["estimated_tokens"],
        "processed_through": span["processed_through"],
        "from_sequence": span["from_sequence"],
        "to_sequence": span["to_sequence"],
        "max_sequence": span["max_sequence"],
        "outstanding": span["outstanding"],
        "relevant": span["relevant"],
        "age_seconds": span.get("age_seconds", 0.0),
        "suggested_trigger": "critical" if blocked else None,
    }


def require_forward_progress(db: Session, campaign_id: uuid.UUID) -> dict[str, Any]:
    """Raise :class:`BackpressureBlocked` when new AI progression must pause."""
    status = evaluate_backpressure(db, campaign_id)
    if status["blocked"]:
        raise BackpressureBlocked(status)
    return status


def request_catchup(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    trigger: str = "critical",
    operation_id: str | None = None,
    commit: bool = True,
):
    """Fire a critical/forced cumulative catch-up trigger (best-effort).

    Thin wrapper over the #216 batch policy: ``critical``/``force`` bypass
    the batching gate so a backpressured campaign converges. Returns the
    run or None when there is nothing outstanding.
    """
    from app.post_turn.service import maybe_trigger_post_turn

    run = maybe_trigger_post_turn(
        db, campaign_id, trigger=trigger, operation_id=operation_id, commit=commit
    )
    if run is not None:
        structured_log(
            logger, logging.INFO, "backpressure_catchup_requested",
            campaign_id=str(campaign_id), trigger=trigger,
            run_id=str(run.id),
            from_sequence=run.from_sequence, to_sequence=run.to_sequence,
        )
    return run


def pause_if_backpressured(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    attempt_id: uuid.UUID | None = None,
    turn_id: uuid.UUID | None = None,
) -> dict[str, Any] | None:
    """Execution gate: return None to proceed, or pause and return status.

    On block: fires a best-effort critical catch-up trigger, commits the
    trigger rows, logs observability (range size, estimated cost, lag age,
    trigger), and returns the status so the caller can defer WITHOUT
    failing the attempt — it stays prepared and the sweep retries after
    catch-up removes the backpressure state automatically.
    """
    try:
        status = evaluate_backpressure(db, campaign_id)
    except Exception as exc:  # noqa: BLE001 — gate itself must fail conservative
        logger.warning("backpressure gate failed campaign=%s error=%s", campaign_id, exc)
        safe_bytes, safe_tokens = get_safe_context_budget()
        status = {
            "campaign_id": str(campaign_id),
            "blocked": True,
            "reason": "estimation_failed",
            "safe_budget_bytes": safe_bytes,
            "safe_budget_tokens": safe_tokens,
            "suggested_trigger": "critical",
        }
    if not status["blocked"]:
        return None
    try:
        request_catchup(db, campaign_id, trigger="critical", commit=False)
        db.commit()
    except Exception as exc:  # noqa: BLE001 — catch-up is best-effort; pause stands regardless
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("backpressure catch-up trigger failed campaign=%s error=%s", campaign_id, exc)
    structured_log(
        logger, logging.WARNING, "backpressure_paused_progression",
        campaign_id=str(campaign_id),
        attempt_id=str(attempt_id) if attempt_id else None,
        turn_id=str(turn_id) if turn_id else None,
        outstanding=status.get("outstanding"),
        estimated_bytes=status.get("estimated_bytes"),
        safe_budget_bytes=status.get("safe_budget_bytes"),
        age_seconds=status.get("age_seconds"),
        reason=status.get("reason"),
        catch_up_trigger="critical",
    )
    return status


def describe_client_state(status: dict[str, Any] | None) -> dict[str, Any]:
    """Low-key client state: ready vs temporary DM processing delay.

    Never exposes queues, providers, models, checkpoints, or any other
    infrastructure detail — just whether play can continue and a generic
    reassurance that accepted input is saved.
    """
    if status is not None and status.get("blocked"):
        return {
            "dm_state": "processing",
            "message": "The DM is finishing processing recent events before continuing. "
            "Your input is saved and play will continue shortly.",
        }
    return {"dm_state": "ready"}
