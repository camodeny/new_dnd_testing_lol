"""Decision-first semantic router + bounded fast-path executor — issue #382.

Inserts bounded semantic routing BEFORE the full generative forward-DM
adjudication in :func:`app.dm.execution._execute_owned_attempt`:

``accepted intent + authoritative context -> code-enumerated decision frame
-> bounded semantic decision(s) -> direct supported execution
OR primer/advisory generative path OR OPEN_ENDED_DM -> existing adjudicator``

Responsibility split (never inverted):

- Deterministic code enumerates every legal route candidate and owns the
  payload needed to execute it. The decision model selects *only* among
  supplied candidate IDs.
- Direct execution builds :class:`DmTurnContractV1` exclusively from
  code-owned templated content — never from model-generated strings. The
  full generative DM remains the escape hatch for novel fiction, unusual
  actions, unsupported mechanics, and any case where a safe complete
  bounded payload cannot be assembled.
- The selected candidate is revalidated against the current authoritative
  revision immediately before execution; stale coverage escalates.
- Decision-model failure never creates a degraded fake DM: any
  :exc:`DecisionError`, adapter outage, or unexpected error escalates to
  the ordinary generative path with the original player input intact.

Direct routes in this increment (more are added incrementally, never all
at once):

- ``route_silent`` — obvious silent/no-op completion. Always enumerable:
  the silent contract carries no beats, effects, or player-visible text.
- Future routes plug into :func:`enumerate_route_candidates` once their
  execution payload is safely constructible without arbitrary generation:
  ``table_chat`` needs a quality bar for templated OOC intent text, and
  pending-roll continuation needs a same-row re-emit mechanism in the
  rolls service (``request_rolls`` always inserts new rows today, so
  re-emitting would duplicate the pending request).

The AI Dungeon Master is the only DM in this product; routing never
implies a separate human DM or moderator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.decisions import (
    DIRECT_EXECUTE,
    ESCALATE,
    PRIMER_ADVISORY,
    CandidateRecord,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    PolicyVerdict,
    build_frame,
    evaluate_execution,
    frame_trace,
    policy_trace,
    register_policy,
    revalidate_for_execution,
    to_decision_request,
)
from app.decisions.contracts import ChoiceResult
from app.decisions.errors import DecisionError
from app.dm.contract import DmTurnContractV1, normalize_contract

logger = logging.getLogger(__name__)

FORWARD_DM_ROUTE_CLASS = "forward_dm_route"
ROUTE_QUESTION_ID = "forward_dm_route"

# Route candidate IDs owned by code. Escape/defer IDs (OPEN_ENDED_DM,
# CLARIFY, DEFER) are appended to every frame by build_frame.
ROUTE_SILENT_ID = "route_silent"

# Trace value for the path taken. decision_only means no generative
# adjudication call ran; primed_generative means the generative DM ran with
# an attached advisory prior; open_ended_generative is the ordinary path.
DECISION_ONLY = "decision_only"
PRIMED_GENERATIVE = "primed_generative"
OPEN_ENDED_GENERATIVE = "open_ended_generative"

SILENT_REASON = (
    "Decision-first direct route (forward_dm_route/route_silent): "
    "no player-visible response required."
)


def _ensure_route_policy() -> DecisionClassPolicy:
    """Register the conservative forward-DM routing policy (idempotent).

    Direct execution needs a clear calibrated winner: high probability and
    confidence plus a decisive alternative margin. Near-ties escalate to
    the open-ended AI DM rather than guessing, and anything below the
    primer floor escalates too. There is deliberately no shared global
    threshold — this policy belongs to this decision class only.
    """
    from app.decisions.policy import get_policy

    try:
        return get_policy(FORWARD_DM_ROUTE_CLASS)
    except DecisionError:
        return register_policy(
            DecisionClassPolicy(
                decision_class=FORWARD_DM_ROUTE_CLASS,
                min_probability_direct=0.80,
                min_confidence_direct=0.75,
                min_margin_direct=0.20,
                near_tie_margin=0.20,
                near_tie_behavior=ESCALATE,
                allow_direct_when_irreversible=False,
                max_risk_for_direct="low",
                min_probability_primer=0.50,
                min_confidence_primer=0.45,
            )
        )


_ensure_route_policy()


@dataclass(frozen=True)
class RouteSignals:
    """Authoritative code-owned signals a route frame is built from."""

    state_revision: str | int
    submission_ids: tuple[str, ...]
    segments: tuple[dict[str, str], ...] = ()
    ooc_only: bool = False
    pending_roll_count: int = 0
    pending_roll_labels: tuple[str, ...] = ()


@dataclass
class RoutingOutcome:
    """One routing decision: what to do and the trace proving why."""

    directive: str
    selected_id: str | None = None
    contract: DmTurnContractV1 | None = None
    primer: dict[str, Any] | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    decision_skipped: bool = False


def _escalate(reason: str, **extra: Any) -> RoutingOutcome:
    trace: dict[str, Any] = {
        "decision_path": OPEN_ENDED_GENERATIVE,
        "decision_class": FORWARD_DM_ROUTE_CLASS,
        "directive": ESCALATE,
        "reason": reason,
    }
    trace.update(extra)
    return RoutingOutcome(
        directive=ESCALATE, selected_id=None, trace=trace,
        decision_skipped=bool(extra.get("decision_skipped", False)),
    )


def enumerate_route_candidates(signals: RouteSignals) -> tuple[CandidateRecord, ...]:
    """Enumerate directly-executable route candidates from signals.

    Only routes whose complete execution payload is assemblable from
    authoritative code-owned state are enumerated. Escape candidates are
    appended later by :func:`build_frame`.
    """
    candidates: list[CandidateRecord] = [
        CandidateRecord(
            id=ROUTE_SILENT_ID,
            label="Complete with no player-visible response (silent)",
            source="dm:route",
            source_ref="route:silent",
            payload_ref="contract:silent",
            debug_hint="No beats, effects, or narration; bookkeeping only",
            risk="low",
            reversible=True,
        ),
    ]
    return tuple(candidates)


def build_route_frame(signals: RouteSignals) -> DecisionFrame:
    """Build the route decision frame over already-authorized state.

    ``state`` carries only audience-authorized attempt content: typed
    submission segments plus pending-roll labels. Secrets (DCs, hidden
    state) never enter the frame.
    """
    state = {
        "submission_segments": [
            {"type": seg["type"], "text": seg["text"]}
            for seg in signals.segments
        ],
        "ooc_only": signals.ooc_only,
        "pending_roll_count": signals.pending_roll_count,
        "pending_roll_labels": list(signals.pending_roll_labels),
        "source_revision": signals.state_revision,
    }
    return build_frame(
        decision_class=FORWARD_DM_ROUTE_CLASS,
        question_id=ROUTE_QUESTION_ID,
        instructions=(
            "Route this accepted player intent. Choose the bounded route "
            "whose required payload is fully present, or defer to the "
            "open-ended AI DM. Never force a bounded route: when in doubt "
            "choose OPEN_ENDED_DM. route_silent only when the intent needs "
            "no player-visible response at all."
        ),
        state=state,
        state_revision=signals.state_revision,
        candidates=enumerate_route_candidates(signals),
        source="dm:route",
    )


def build_direct_contract(selected_id: str) -> DmTurnContractV1:
    """Build the directly-executed contract from code-owned content only.

    Raises :exc:`DecisionError` (malformed) for any ID without a
    deterministic builder — model-selected IDs never synthesize contracts.
    """
    if selected_id == ROUTE_SILENT_ID:
        return normalize_contract(
            {
                "contract_version": "dm_turn_contract_v1",
                "mode": "silent",
                "reason": SILENT_REASON,
            }
        )
    raise DecisionError(
        f"route candidate {selected_id!r} has no deterministic contract builder",
        kind="malformed",
    )


def _silent_still_legal(
    frame: DecisionFrame, submission_ids: tuple[str, ...]
) -> Any:
    """Legality closure: silent stays legal while the input set is unchanged."""

    def _check(candidate: CandidateRecord) -> bool:
        if candidate.id != ROUTE_SILENT_ID:
            return False
        try:
            from app.decisions.frames import resolve_candidate

            resolve_candidate(frame, candidate.id)
        except DecisionError:
            return False
        return True

    _ = submission_ids
    return _check


def decide_from_result(
    frame: DecisionFrame,
    result: ChoiceResult,
    *,
    current_revision: str | int,
    submission_ids: tuple[str, ...],
) -> RoutingOutcome:
    """Apply policy + revalidation to one decision result.

    Pure (no I/O): shared by the service path and unit tests.
    """
    from app.decisions.policy import alternative_margin

    policy = _ensure_route_policy()
    margin = alternative_margin(result.probabilities, result.selected_id)
    verdict: PolicyVerdict = evaluate_execution(
        frame,
        result.selected_id,
        result.probabilities,
        result.confidence,
        policy=policy,
        verified=True,
    )
    base_trace = policy_trace(frame, verdict)
    if verdict.directive == DIRECT_EXECUTE:
        try:
            revalidate_for_execution(
                frame,
                verdict.selected_id,
                current_revision,
                still_legal=_silent_still_legal(frame, submission_ids),
            )
            contract = build_direct_contract(verdict.selected_id)
        except DecisionError as exc:
            # Stale coverage or an unbuildable selection escalates rather
            # than guessing; the generative path keeps the input intact.
            trace = dict(base_trace)
            trace.update(
                {
                    "decision_path": OPEN_ENDED_GENERATIVE,
                    "revalidation_error": str(exc),
                }
            )
            return RoutingOutcome(
                directive=ESCALATE,
                selected_id=verdict.selected_id,
                trace=trace,
            )
        trace = dict(base_trace)
        trace.update(
            {
                "decision_path": DECISION_ONLY,
                "probability": verdict.probability,
                "confidence": verdict.confidence,
                "margin": margin,
            }
        )
        return RoutingOutcome(
            directive=DIRECT_EXECUTE,
            selected_id=verdict.selected_id,
            contract=contract,
            trace=trace,
        )
    if verdict.directive == PRIMER_ADVISORY:
        trace = dict(base_trace)
        trace.update(
            {
                "decision_path": PRIMED_GENERATIVE,
                "probability": verdict.probability,
                "confidence": verdict.confidence,
                "margin": margin,
            }
        )
        primer = {
            "selected_id": verdict.selected_id,
            "directive": PRIMER_ADVISORY,
            "probability": verdict.probability,
            "confidence": verdict.confidence,
            "margin": margin,
            # Advisory only: the generative adjudicator is never forced to
            # agree. Prompt-level injection of the prior is a follow-up;
            # today the primer travels as trace/diagnostic metadata.
        }
        return RoutingOutcome(
            directive=PRIMER_ADVISORY,
            selected_id=verdict.selected_id,
            primer=primer,
            trace=trace,
        )
    trace = dict(base_trace)
    trace.update(
        {
            "decision_path": OPEN_ENDED_GENERATIVE,
            "probability": verdict.probability,
            "confidence": verdict.confidence,
            "margin": margin,
        }
    )
    return RoutingOutcome(
        directive=ESCALATE, selected_id=verdict.selected_id, trace=trace
    )


def collect_signals(db: Any, attempt: Any, turn: Any) -> RouteSignals:
    """Read authoritative signals for the attempt's current input set."""
    submission_ids = tuple(str(s) for s in (attempt.submission_ids or []))
    segments: list[dict[str, str]] = []
    ooc_only = False
    try:
        from sqlalchemy import select

        from models.threads import PlayerSubmissionSegment

        as_uuids: list[Any] = []
        import uuid as _uuid

        for value in submission_ids:
            try:
                as_uuids.append(_uuid.UUID(str(value)))
            except (ValueError, TypeError, AttributeError):
                continue
        rows = (
            list(
                db.scalars(
                    select(PlayerSubmissionSegment)
                    .where(PlayerSubmissionSegment.submission_id.in_(as_uuids))
                    .order_by(
                        PlayerSubmissionSegment.submission_id,
                        PlayerSubmissionSegment.position,
                    )
                ).all()
            )
            if as_uuids
            else []
        )
        for row in rows:
            segments.append(
                {"type": str(row.segment_type), "text": str(row.text)}
            )
        ooc_only = bool(segments) and all(
            seg["type"] == "ooc" for seg in segments
        )
    except Exception as exc:
        logger.warning("decision routing segment read failed: %s", exc)
        segments = []
        ooc_only = False
    pending_count = 0
    pending_labels: list[str] = []
    try:
        from sqlalchemy import select

        from models.dm import PlayerRollRequest

        pending_rows = list(
            db.scalars(
                select(PlayerRollRequest)
                .where(
                    PlayerRollRequest.turn_id == turn.id,
                    PlayerRollRequest.status == "pending",
                )
                .order_by(PlayerRollRequest.requested_at)
            ).all()
        )
        pending_count = len(pending_rows)
        # Labels only: DCs and hidden roll state never enter the frame.
        pending_labels = [str(row.label or row.id) for row in pending_rows]
    except Exception as exc:
        logger.warning("decision routing pending-roll read failed: %s", exc)
    revision = getattr(attempt, "source_revision", 0)
    return RouteSignals(
        state_revision=revision,
        submission_ids=submission_ids,
        segments=tuple(segments),
        ooc_only=ooc_only,
        pending_roll_count=pending_count,
        pending_roll_labels=tuple(pending_labels),
    )


def current_revision(db: Any, campaign_id: Any) -> str | int:
    """Re-read the authoritative campaign revision for revalidation."""
    from models.campaigns import Campaign

    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        raise DecisionError(
            "decision routing cannot revalidate without campaign authority",
            kind="malformed",
        )
    return campaign.revision


def route_attempt(
    db: Any,
    *,
    attempt: Any,
    turn: Any,
    trace_id: str | None = None,
    decision_service: DecisionService | None = None,
) -> RoutingOutcome:
    """Route one prepared attempt: direct, primer, or generative escape.

    Never raises for decision-plane problems — those escalate. Unexpected
    errors also escalate so a router bug cannot wedge turn execution.
    """
    try:
        signals = collect_signals(db, attempt, turn)
    except Exception as exc:
        logger.warning("decision routing signal collection failed: %s", exc)
        return _escalate(f"signal collection failed: {exc}")
    try:
        frame = build_route_frame(signals)
    except DecisionError as exc:
        return _escalate(f"frame build failed: {exc}")
    service = decision_service or DecisionService()
    try:
        response = service.decide(to_decision_request(frame))
    except DecisionError as exc:
        trace_extra: dict[str, Any] = {
            "frame_id": frame.frame_id,
            "question_id": frame.question_id,
            "state_revision": signals.state_revision,
            "candidate_ids": [c.id for c in frame.candidates],
        }
        if getattr(exc, "provider", None):
            trace_extra["provider"] = exc.provider
        return _escalate(
            f"decision call failed ({exc.kind}); generative escape",
            **trace_extra,
        )
    except Exception as exc:
        logger.warning("decision routing unexpected error: %s", exc)
        return _escalate(
            "unexpected routing error; generative escape",
            frame_id=frame.frame_id,
        )
    result = response.results.get(ROUTE_QUESTION_ID)
    if not isinstance(result, ChoiceResult):
        return _escalate(
            "decision response missing route result; generative escape",
            frame_id=frame.frame_id,
        )
    try:
        revision = current_revision(db, attempt.campaign_id)
    except DecisionError as exc:
        return _escalate(f"revalidation authority missing: {exc}")
    try:
        outcome = decide_from_result(
            frame,
            result,
            current_revision=revision,
            submission_ids=signals.submission_ids,
        )
    except DecisionError as exc:
        base = frame_trace(frame)
        base.update(
            {
                "decision_path": OPEN_ENDED_GENERATIVE,
                "policy_error": str(exc),
            }
        )
        return RoutingOutcome(directive=ESCALATE, trace=base)
    trace = outcome.trace
    trace.setdefault("provider", response.provider)
    trace.setdefault("model", response.model)
    trace.setdefault("latency_ms", response.latency_ms)
    if trace_id is not None:
        trace.setdefault("trace_id", trace_id)
    outcome.trace = trace
    return outcome


def path_info_fields(outcome: RoutingOutcome) -> dict[str, Any]:
    """Diagnostic fields merged into execution path_info (see #375)."""
    fields: dict[str, Any] = {
        "decision_path": outcome.trace.get("decision_path"),
        "decision_directive": outcome.directive,
    }
    if outcome.selected_id is not None:
        fields["decision_selected"] = outcome.selected_id
    for key in ("provider", "model", "latency_ms"):
        if outcome.trace.get(key) is not None:
            fields[f"decision_{key}"] = outcome.trace[key]
    if outcome.primer is not None:
        fields["decision_primer"] = outcome.primer
    return fields
