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
    ACTIVE,
    DIRECT_EXECUTE,
    ESCALATE,
    PRIMER,
    PRIMER_ADVISORY,
    SHADOW,
    CandidateRecord,
    DecisionClassPolicy,
    DecisionFrame,
    DecisionService,
    PolicyVerdict,
    build_frame,
    build_record,
    evaluate_execution,
    frame_trace,
    policy_trace,
    record_fail_soft,
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
    """Authoritative code-owned signals a route frame is built from.

    ``complete`` is False when any authoritative read failed or coverage is
    incomplete (e.g. submissions exist but no segments were read). An
    incomplete signal set must escalate to the generative path — never
    execute directly against a frame that may be missing player input or
    pending player-owned rolls.
    """

    state_revision: str | int
    submission_ids: tuple[str, ...]
    segments: tuple[dict[str, str], ...] = ()
    ooc_only: bool = False
    pending_roll_count: int = 0
    pending_roll_labels: tuple[str, ...] = ()
    complete: bool = True
    signal_error: str | None = None
    # Attempt audience scope (issue #248): the frame state is already
    # audience-authorized content, and the audience marker lets downstream
    # consumers (primer attachment, telemetry) keep private scope without
    # re-reading authority.
    audience: str = "campaign"


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
        "audience": signals.audience,
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


def _silent_still_legal(frame: DecisionFrame) -> Any:
    """Legality closure: the selection must belong to the frame.

    Input-set drift is checked separately by comparing the frame-time and
    freshly re-read submission sets in :func:`decide_from_result`.
    """

    def _check(candidate: CandidateRecord) -> bool:
        if candidate.id != ROUTE_SILENT_ID:
            return False
        try:
            from app.decisions.frames import resolve_candidate

            resolve_candidate(frame, candidate.id)
        except DecisionError:
            return False
        return True

    return _check


def decide_from_result(
    frame: DecisionFrame,
    result: ChoiceResult,
    *,
    current_revision: str | int,
    frame_submission_ids: tuple[str, ...] = (),
    current_submission_ids: tuple[str, ...] | None = None,
    submission_ids: tuple[str, ...] = (),
) -> RoutingOutcome:
    """Apply policy + revalidation to one decision result.

    Pure (no I/O): shared by the service path and unit tests.
    ``frame_submission_ids`` is the input set the frame was enumerated
    against; ``current_submission_ids`` is the freshly re-read set (defaults
    to ``submission_ids`` for backward-compatible callers). Any drift
    escalates instead of executing against stale input.
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
    # Backward-compatible default: callers that pass one set assert it is
    # both the frame-time and the current set.
    frame_ids = tuple(frame_submission_ids) or tuple(submission_ids)
    live_ids = (
        tuple(current_submission_ids)
        if current_submission_ids is not None
        else tuple(submission_ids)
    )
    if verdict.directive == DIRECT_EXECUTE:
        try:
            if frame_ids != live_ids:
                raise DecisionError(
                    "decision frame input set changed during the decision call; "
                    "escalating to the generative path",
                    kind="stale",
                )
            revalidate_for_execution(
                frame,
                verdict.selected_id,
                current_revision,
                still_legal=_silent_still_legal(frame),
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
            # Advisory only: execution attaches this to the adjudication
            # packet via attach_primer, and the generative adjudicator is
            # never forced to agree.
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
    complete = True
    signal_error: str | None = None
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
        complete = False
        signal_error = f"submission segment read failed: {exc}"
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
        complete = False
        signal_error = f"pending roll read failed: {exc}"
    if complete and submission_ids and not segments:
        # Context assembly guarantees typed segments for every submission,
        # so an empty read against a non-empty input set is incomplete
        # coverage, not a genuine no-input attempt.
        complete = False
        signal_error = "no submission segments read for a non-empty input set"
    revision = getattr(attempt, "source_revision", 0)
    audience = str(getattr(attempt, "audience", None) or "campaign")
    return RouteSignals(
        state_revision=revision,
        submission_ids=submission_ids,
        segments=tuple(segments),
        ooc_only=ooc_only,
        pending_roll_count=pending_count,
        pending_roll_labels=tuple(pending_labels),
        complete=complete,
        signal_error=signal_error,
        audience=audience,
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


def assert_attempt_current(db: Any, attempt: Any) -> tuple[str, ...]:
    """Require the attempt to still own its turn's input set.

    Re-reads the turn and attempt rows (refreshing cached ORM state) and
    requires this attempt to still be ``turn.current_attempt_id`` with the
    same ``input_set_revision`` and submission set. A superseding
    submission — which does not bump campaign revision — otherwise lets a
    decision made on stale input resolve the old attempt and strand the
    player's newer input. Returns the freshly read submission IDs.
    Raises :exc:`DecisionError` (stale) on any drift.
    """
    from models.dm import DmTurn, DmTurnAttempt

    try:
        db.refresh(attempt)
    except Exception:
        pass
    fresh_attempt = db.get(DmTurnAttempt, attempt.id)
    if fresh_attempt is None:
        raise DecisionError(
            "decision routing attempt disappeared; escalating",
            kind="stale",
        )
    try:
        db.refresh(fresh_attempt)
    except Exception:
        pass
    turn = db.get(DmTurn, fresh_attempt.turn_id)
    if turn is None:
        raise DecisionError(
            "decision routing turn is missing; escalating",
            kind="stale",
        )
    try:
        db.refresh(turn)
    except Exception:
        pass
    if turn.current_attempt_id != fresh_attempt.id:
        raise DecisionError(
            "decision routing attempt is no longer the current attempt; "
            "a newer submission superseded it",
            kind="stale",
        )
    if turn.input_set_revision != fresh_attempt.input_set_revision:
        raise DecisionError(
            "decision routing input set revision moved on; escalating",
            kind="stale",
        )
    if list(turn.submission_ids or []) != list(fresh_attempt.submission_ids or []):
        raise DecisionError(
            "decision routing submission set changed; escalating",
            kind="stale",
        )
    if str(getattr(fresh_attempt, "status", "")) in (
        "superseded",
        "abandoned",
        "discarded",
        "succeeded",
        "failed_visible",
        "failed",
    ):
        raise DecisionError(
            f"decision routing attempt is {fresh_attempt.status}; escalating",
            kind="stale",
        )
    # Resumed player-roll attempts carry fulfilled roll outcomes as evidence
    # for the generative adjudicator. There is no bounded roll-continuation
    # route yet, so any non-empty roll evidence escalates: silently
    # resolving here would discard the player-owned roll outcome.
    if fresh_attempt.roll_evidence:
        raise DecisionError(
            "decision routing defers to the generative path when resumed "
            "roll evidence is present",
            kind="stale",
        )
    return tuple(str(s) for s in (fresh_attempt.submission_ids or []))


def _fresh_roll_evidence(db: Any, attempt: Any) -> list[Any]:
    """Re-read resumed roll evidence (refreshing cached ORM state)."""
    from models.dm import DmTurnAttempt

    try:
        db.refresh(attempt)
    except Exception:
        pass
    fresh = db.get(DmTurnAttempt, attempt.id)
    if fresh is None:
        return []
    try:
        db.refresh(fresh)
    except Exception:
        pass
    return list(fresh.roll_evidence or [])


def _private_authorization_error(db: Any, attempt: Any, turn: Any) -> str | None:
    """Verify the attempt may build a decision frame on its thread (issue #248).

    Decision frames/candidates are built only after private-thread
    authorization: the attempt audience must match its authoritative thread
    type on the same campaign. Returns a reason string when the frame must
    NOT be built (the caller escalates to the generative path, which
    re-authorizes through context assembly), or None when authorized.

    Never raises: any authority-read failure escalates rather than guessing.
    Logs metadata/IDs only — never submission text or hidden state.
    """
    try:
        import uuid as _uuid

        from models.threads import CampaignThread

        attempt_audience = str(getattr(attempt, "audience", None) or "campaign")
        turn_audience = str(getattr(turn, "audience", None) or "campaign")
        if attempt_audience != turn_audience:
            logger.warning(
                "decision routing audience mismatch attempt_id=%s turn_id=%s attempt_audience=%s turn_audience=%s",
                getattr(attempt, "id", None), getattr(turn, "id", None),
                attempt_audience, turn_audience,
            )
            return "attempt audience does not match its turn audience"
        try:
            thread_uuid = _uuid.UUID(str(getattr(attempt, "thread_id", "")))
        except (ValueError, TypeError, AttributeError):
            return "attempt thread id is not a valid thread"
        thread = db.get(CampaignThread, thread_uuid)
        if thread is None:
            logger.warning(
                "decision routing thread missing attempt_id=%s turn_id=%s",
                getattr(attempt, "id", None), getattr(turn, "id", None),
            )
            return "attempt thread authority is missing"
        if str(thread.campaign_id) != str(getattr(attempt, "campaign_id", "")):
            return "attempt thread belongs to another campaign"
        if str(thread.thread_type) != attempt_audience:
            logger.warning(
                "decision routing thread audience mismatch attempt_id=%s thread_id=%s thread_type=%s attempt_audience=%s",
                getattr(attempt, "id", None), thread_uuid,
                thread.thread_type, attempt_audience,
            )
            return "attempt audience does not match its authoritative thread type"
        return None
    except Exception as exc:
        logger.warning("decision routing authorization check failed: %s", exc)
        return f"authorization authority unreadable: {exc}"


PRIMER_RECORD_PREFIX = "decision-primer:"


def attach_primer(packet: Any, primer: dict[str, Any]) -> Any:
    """Attach a primer advisory to the adjudication packet (non-authoritative).

    Returns a copy of the packet with one ``adjudication_only`` record
    appended to the player-inputs lane, so the generative adjudicator can
    observe the bounded route lean without being forced to agree. The
    record is idempotent by stable record ID and never reaches narration
    (``adjudication_only`` records are stripped from the narration
    projection). Raises :exc:`DecisionError` (malformed) when the packet
    shape is unexpected — callers escalate without the primer in that case.
    """
    from app.dm.context import (
        AuthorizationScope,
        ContextRecord,
        LaneName,
        SourceRef,
    )

    frame_id = str(primer.get("frame_id") or "unknown")
    record_id = f"{PRIMER_RECORD_PREFIX}{frame_id}"
    try:
        audience = packet.audience
        lanes = list(packet.lanes)
        lane_index = next(
            i for i, lane in enumerate(lanes) if lane.name == LaneName.PLAYER_INPUTS
        )
    except Exception as exc:
        raise DecisionError(
            f"primer attachment needs a player-inputs lane: {exc}",
            kind="malformed",
        ) from exc
    if any(r.record_id == record_id for r in lanes[lane_index].records):
        return packet
    # Issue #248 — the primer advisory inherits the packet audience: on a
    # private turn it stays private/thread-scoped (never campaign-visible),
    # so the advisory prior cannot widen private routing metadata.
    packet_audience = getattr(packet, "audience", None)
    audience_kind = str(getattr(packet_audience, "audience", "campaign") or "campaign")
    thread_id = str(getattr(packet_audience, "thread_id", "") or "")
    if audience_kind == "private":
        if not thread_id:
            raise DecisionError(
                "primer attachment needs a thread-scoped private audience",
                kind="malformed",
            )
        primer_visibility = "private"
        primer_auth = AuthorizationScope(
            campaign_id=str(getattr(packet_audience, "campaign_id", "")),
            thread_ids=[thread_id],
            user_ids=[],
        )
    else:
        primer_visibility = "campaign"
        primer_auth = AuthorizationScope(
            campaign_id=str(audience.campaign_id),
            thread_ids=[str(audience.thread_id)],
            user_ids=[],
        )
    record = ContextRecord(
        record_id=record_id,
        value={
            "advisory_route": primer.get("selected_id"),
            "label": f"Bounded routing leans toward {primer.get('selected_id')}",
            "probability": primer.get("probability"),
            "confidence": primer.get("confidence"),
            "margin": primer.get("margin"),
            "authority": (
                "advisory only: the adjudicator weighs this prior against "
                "the full authoritative context and is not bound by it"
            ),
        },
        sources=[
            SourceRef(
                source_type="decision_primer",
                source_id=frame_id,
                source_version="1",
                campaign_revision=None,
                provenance={"decision_class": FORWARD_DM_ROUTE_CLASS},
            )
        ],
        authorization=primer_auth,
        visibility=primer_visibility,  # type: ignore[arg-type]
        use="adjudication_only",
        required=False,
        priority=5,
    )
    primed = packet.model_copy(deep=True)
    primed.lanes[lane_index].records.append(record)
    return primed


def _record_routing_telemetry(
    db: Any,
    *,
    frame: DecisionFrame,
    result: Any,
    response: Any,
    outcome: RoutingOutcome,
    mode: str,
    trace_id: str | None = None,
    campaign_id: Any = None,
    turn_id: Any = None,
    session_factory: Any = None,
) -> None:
    """Persist one routing decision telemetry row, fail-soft (issue #383).

    Never raises: observability failure must not mutate or duplicate
    gameplay, so every error path is swallowed after a warning. Only
    stable candidate IDs reach the row — labels, state, and primer
    content stay out.

    The recorded ``policy_directive`` is the original execution-policy
    outcome from the trace — not the post-revalidation routing outcome —
    so a ``direct_execute`` policy verdict that fails deterministic
    revalidation (and therefore escalates) is stored as
    ``direct_execute`` + ``verified=False`` + ``revalidation_error``.
    When ``session_factory`` is omitted it is derived from ``db``.
    """
    try:
        trace = outcome.trace or {}
        selected_id = outcome.selected_id or getattr(result, "selected_id", "")
        policy_directive = trace.get("directive") or outcome.directive
        verdict = PolicyVerdict(
            directive=policy_directive,
            reason=str(trace.get("reason", "")),
            decision_class=frame.decision_class,
            selected_id=selected_id,
            probability=float(trace.get("probability", 0.0)),
            confidence=float(trace.get("confidence", 0.0)),
            margin=float(trace.get("margin", 0.0)),
        )
        revalidation_error = trace.get("revalidation_error")
        if outcome.directive == DIRECT_EXECUTE:
            verified: bool | None = True
        elif revalidation_error is not None:
            verified = False
        else:
            verified = None
        record = build_record(
            frame,
            result,
            verdict,
            provider=getattr(response, "provider", "unknown"),
            model=getattr(response, "model", None) or "unknown",
            mode=mode,
            trace_id=trace_id or trace.get("trace_id") or getattr(response, "trace_id", None),
            operation_id=getattr(response, "operation_id", None) or trace.get("trace_id"),
            campaign_id=campaign_id,
            turn_id=turn_id,
            latency_ms=getattr(response, "latency_ms", None),
            cost_usd=(getattr(response, "usage", None) or {}).get("cost_usd"),
            verified=verified,
            revalidation_error=revalidation_error,
        )
        factory = session_factory
        if factory is None:
            try:
                from app.observability.service import telemetry_factory_for

                factory = telemetry_factory_for(db)
            except Exception:
                factory = None
        record_fail_soft(factory, record)
    except Exception as exc:
        logger.warning("decision routing telemetry dropped: %s", exc)


def _record_superseded_telemetry(
    db: Any,
    *,
    frame: DecisionFrame,
    result: Any,
    response: Any,
    revision: str | int,
    signals: RouteSignals,
    error: Exception,
    shadow: bool,
    trace_id: str | None,
    attempt: Any,
    turn: Any,
) -> None:
    """Record an evaluated decision lost to attempt supersession (issue #383).

    Best-effort and fail-soft: the authoritative escalation is unchanged.
    The execution-policy outcome is derived from a pure re-evaluation probe
    against the frame-time input set; the supersession failure is attached
    as the deterministic revalidation result (``verified=False``). A probe
    of its own that already failed revalidation keeps its own error.
    """
    try:
        probe = decide_from_result(
            frame,
            result,
            current_revision=revision,
            frame_submission_ids=signals.submission_ids,
            current_submission_ids=signals.submission_ids,
        )
    except Exception as probe_exc:
        logger.warning("decision supersession telemetry probe dropped: %s", probe_exc)
        return
    trace = dict(probe.trace)
    trace.update({"decision_path": OPEN_ENDED_GENERATIVE})
    trace.setdefault("revalidation_error", str(error))
    tele_outcome = RoutingOutcome(
        directive=ESCALATE, selected_id=probe.selected_id, trace=trace,
    )
    if shadow:
        mode = SHADOW
    elif probe.directive == PRIMER_ADVISORY:
        mode = PRIMER
    else:
        mode = ACTIVE
    _record_routing_telemetry(
        db,
        frame=frame,
        result=result,
        response=response,
        outcome=tele_outcome,
        mode=mode,
        trace_id=trace_id or getattr(response, "trace_id", None),
        campaign_id=getattr(attempt, "campaign_id", None),
        turn_id=getattr(turn, "id", None) or getattr(attempt, "turn_id", None),
    )


def route_attempt(
    db: Any,
    *,
    attempt: Any,
    turn: Any,
    trace_id: str | None = None,
    decision_service: DecisionService | None = None,
    shadow: bool = False,
) -> RoutingOutcome:
    """Route one prepared attempt: direct, primer, or generative escape.

    Never raises for decision-plane problems — those escalate. Unexpected
    errors also escalate so a router bug cannot wedge turn execution.

    ``shadow`` evaluates the bounded decision and records it as shadow
    telemetry, but the authoritative path continues unchanged: the outcome
    always escalates to the ordinary generative path (issue #383).
    """
    # Resumed player-roll attempts carry fulfilled outcomes as evidence for
    # the generative adjudicator; there is no bounded roll-continuation
    # route yet, so gate before spending a decision call.
    try:
        if _fresh_roll_evidence(db, attempt):
            return _escalate(
                "resumed roll evidence present; generative path preserves "
                "the player-owned roll outcome",
                decision_skipped=True,
            )
    except Exception as exc:
        logger.warning("decision routing evidence gate failed: %s", exc)
    # Issue #248 — private-thread authorization precedes frame/candidate
    # assembly: a bounded decision must never be evaluated on input the
    # attempt is not authorized to see. Failure escalates to the generative
    # path (which re-authorizes through context assembly), never executes.
    try:
        auth_error = _private_authorization_error(db, attempt, turn)
    except Exception as exc:
        logger.warning("decision routing authorization gate failed: %s", exc)
        auth_error = f"authorization gate failed: {exc}"
    if auth_error is not None:
        return _escalate(
            f"attempt authorization failed ({auth_error}); generative escape",
            decision_skipped=True,
        )
    try:
        signals = collect_signals(db, attempt, turn)
    except Exception as exc:
        logger.warning("decision routing signal collection failed: %s", exc)
        return _escalate(f"signal collection failed: {exc}")
    if not signals.complete:
        # Incomplete coverage must escalate rather than guess: a degraded
        # read could hide player input or a pending player-owned roll, and
        # route_silent would otherwise resolve the attempt unseen.
        return _escalate(
            f"incomplete routing signals: {signals.signal_error}; "
            "generative escape",
            decision_skipped=True,
            signal_error=signals.signal_error,
        )
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
        fresh_submission_ids = assert_attempt_current(db, attempt)
    except DecisionError as exc:
        base = frame_trace(frame)
        base.update(
            {
                "decision_path": OPEN_ENDED_GENERATIVE,
                "revalidation_error": str(exc),
            }
        )
        # The bounded decision was already evaluated: record its policy
        # outcome plus this deterministic revalidation failure (fail-soft)
        # while the authoritative path escalates unchanged.
        _record_superseded_telemetry(
            db,
            frame=frame,
            result=result,
            response=response,
            revision=revision,
            signals=signals,
            error=exc,
            shadow=shadow,
            trace_id=trace_id,
            attempt=attempt,
            turn=turn,
        )
        return RoutingOutcome(directive=ESCALATE, trace=base)
    try:
        outcome = decide_from_result(
            frame,
            result,
            current_revision=revision,
            frame_submission_ids=signals.submission_ids,
            current_submission_ids=fresh_submission_ids,
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
    if outcome.primer is not None:
        outcome.primer["frame_id"] = frame.frame_id
    trace = outcome.trace
    trace.setdefault("provider", response.provider)
    trace.setdefault("model", response.model)
    trace.setdefault("latency_ms", response.latency_ms)
    effective_trace_id = trace_id or getattr(response, "trace_id", None)
    if effective_trace_id is not None:
        trace.setdefault("trace_id", effective_trace_id)
    outcome.trace = trace
    # Issue #383 — record the evaluated decision (shadow vs active/primer)
    # without ever disturbing the routing outcome. Telemetry is fail-soft.
    live_mode = PRIMER if outcome.directive == PRIMER_ADVISORY else ACTIVE
    _record_routing_telemetry(
        db,
        frame=frame,
        result=result,
        response=response,
        outcome=outcome,
        mode=SHADOW if shadow else live_mode,
        trace_id=effective_trace_id,
        campaign_id=getattr(attempt, "campaign_id", None),
        turn_id=getattr(turn, "id", None) or getattr(attempt, "turn_id", None),
    )
    if shadow:
        # Authoritative path continues unchanged: discard any direct
        # contract or primer and escalate to the generative path.
        shadow_trace = dict(trace)
        shadow_trace.update(
            {
                "decision_path": OPEN_ENDED_GENERATIVE,
                "shadow": True,
                "shadow_selected": outcome.selected_id,
                "shadow_directive": outcome.directive,
                "reason": "shadow evaluation only; authoritative path unchanged",
            }
        )
        return RoutingOutcome(
            directive=ESCALATE,
            selected_id=outcome.selected_id,
            trace=shadow_trace,
        )
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
