"""Decision telemetry — issue #383.

Every bounded semantic decision is recorded so it can be measured against
later authoritative outcomes.

Responsibility split (never inverted):

- Deterministic code enumerates candidates, owns authorization/legality, and
  performs revalidation. Decision models choose only among code-supplied
  candidate IDs. Model confidence is evidence for the execution policy —
  never authorization.
- Telemetry is observability only: persistence failures never mutate or
  duplicate gameplay. All writes go through short independent transactions
  (a session factory, never a gameplay ``Session``) and fail soft.
- Privacy: :func:`shared_trace` exposes only stable IDs, versions, and
  numbers. Candidate labels, debug hints, provenance refs, payload refs,
  and raw decision state never enter telemetry rows or shared traces.

The AI Dungeon Master is the only DM in this product; no telemetry,
trace, or summary copy may imply a separate human DM or moderator.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from app.decisions.contracts import ChoiceResult
from app.decisions.errors import DecisionError
from app.decisions.frames import (
    CANDIDATE_SCHEMA_VERSION,
    DecisionFrame,
    FRAME_SCHEMA_VERSION,
)
from app.decisions.policy import (
    POLICY_SCHEMA_VERSION,
    PolicyVerdict,
    alternative_margin,
)

logger = logging.getLogger(__name__)

TELEMETRY_SCHEMA_VERSION = 1

# Execution modes: how the decision ran. Shadow evaluations are recorded
# but never drive gameplay; primer runs are advisory input to a
# deterministic/generative step; active runs may directly execute.
SHADOW = "shadow"
PRIMER = "primer"
ACTIVE = "active"
EXECUTION_MODES = frozenset({SHADOW, PRIMER, ACTIVE})


def _check_mode(mode: str) -> str:
    if mode not in EXECUTION_MODES:
        raise DecisionError(
            f"unknown decision execution mode {mode!r}; "
            f"expected one of {sorted(EXECUTION_MODES)}",
            kind="malformed",
        )
    return mode


def runner_up(
    probabilities: Mapping[str, float], selected_id: str
) -> tuple[str | None, float]:
    """Return ``(runner_up_id, margin)`` for a selected candidate.

    ``margin`` is top-1 minus top-2 probability (0.0 for a single-candidate
    map). Ties for second place resolve deterministically by sorted ID so
    repeated evaluations produce identical telemetry.
    """
    top = float(probabilities[selected_id])
    best_id: str | None = None
    best_value = 0.0
    for candidate_id in sorted(probabilities):
        if candidate_id == selected_id:
            continue
        value = float(probabilities[candidate_id])
        if best_id is None or value > best_value:
            best_id, best_value = candidate_id, value
    return best_id, max(0.0, top - best_value)


@dataclass(frozen=True)
class DecisionRecord:
    """One evaluated decision question with full reconstruction context.

    ``decision_class`` is the decision role (per-role calibration keys on
    it). ``probabilities`` is the full distribution where available.
    ``ground_truth_id`` is definitive authoritative outcome when known;
    correction fields are delayed wrongness evidence (validator/repair
    signals) and stay distinct from ground truth.
    """

    decision_class: str
    question_id: str
    question_kind: str
    provider: str
    model: str
    candidate_ids: tuple[str, ...]
    selected_id: str
    probabilities: dict[str, float] = field(default_factory=dict)
    runner_up_id: str | None = None
    margin: float = 0.0
    confidence: float | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    trace_id: str = ""
    operation_id: str | None = None
    campaign_id: str | None = None
    turn_id: str | None = None
    frame_id: str = ""
    state_revision: str = ""
    mode: str = ACTIVE
    policy_directive: str | None = None
    verified: bool | None = None
    revalidation_error: str | None = None
    model_version: str | None = None
    candidate_schema_version: int = CANDIDATE_SCHEMA_VERSION
    frame_schema_version: int = FRAME_SCHEMA_VERSION
    policy_schema_version: int = POLICY_SCHEMA_VERSION
    telemetry_schema_version: int = TELEMETRY_SCHEMA_VERSION
    ground_truth_id: str | None = None
    correction_source: str | None = None
    correction_indicates_wrong: bool | None = None
    corrected_to: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision_class, str) or not self.decision_class.strip():
            raise DecisionError("telemetry record is missing a decision class", kind="malformed")
        _check_mode(self.mode)


def build_record(
    frame: DecisionFrame,
    result: ChoiceResult,
    verdict: PolicyVerdict,
    *,
    provider: str,
    model: str,
    mode: str = ACTIVE,
    trace_id: str | None = None,
    operation_id: str | None = None,
    campaign_id: Any = None,
    turn_id: Any = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    verified: bool | None = None,
    revalidation_error: str | None = None,
    model_version: str | None = None,
) -> DecisionRecord:
    """Assemble a telemetry record from one evaluated frame.

    Pure (no I/O): the frame, the typed adapter result, and the policy
    verdict together reconstruct which candidates/question/policy/model
    produced the selected path. Only stable candidate IDs are retained —
    labels, debug hints, provenance/payload refs, and raw state never
    enter the record.
    """
    _check_mode(mode)
    if result.question_id != frame.question_id:
        raise DecisionError(
            f"telemetry result {result.question_id!r} does not belong to frame "
            f"question {frame.question_id!r}",
            kind="malformed",
        )
    distribution = dict(result.probabilities)
    second_id, margin = runner_up(distribution, result.selected_id)
    # Cross-check the policy-computed margin so a sliced map can never
    # inflate telemetry: both derive from the same distribution.
    policy_margin = alternative_margin(distribution, result.selected_id)
    if abs(margin - policy_margin) > 1e-9:  # pragma: no cover - arithmetic guard
        raise DecisionError("telemetry margin contradicts policy margin", kind="malformed")
    return DecisionRecord(
        decision_class=frame.decision_class,
        question_id=frame.question_id,
        question_kind="choice",
        provider=provider,
        model=model,
        candidate_ids=tuple(c.id for c in frame.candidates),
        selected_id=result.selected_id,
        probabilities=distribution,
        runner_up_id=second_id,
        margin=margin,
        confidence=result.confidence,
        latency_ms=latency_ms,
        cost_usd=cost_usd,
        trace_id=trace_id or "",
        operation_id=operation_id,
        campaign_id=str(campaign_id) if campaign_id is not None else None,
        turn_id=str(turn_id) if turn_id is not None else None,
        frame_id=frame.frame_id,
        state_revision=str(frame.state_revision),
        mode=mode,
        policy_directive=verdict.directive,
        verified=verified,
        revalidation_error=revalidation_error,
        model_version=model_version,
    )


def shared_trace(record: DecisionRecord) -> dict[str, Any]:
    """Privacy-safe trace for default shared/debug logging.

    Contains only stable IDs, schema/policy/model versions, and numeric
    evidence. Candidate labels, debug hints, provenance/payload refs, raw
    decision state, and correction details never appear here.
    """
    return {
        "decision_class": record.decision_class,
        "question_id": record.question_id,
        "question_kind": record.question_kind,
        "provider": record.provider,
        "model": record.model,
        "model_version": record.model_version,
        "candidate_schema_version": record.candidate_schema_version,
        "frame_schema_version": record.frame_schema_version,
        "policy_schema_version": record.policy_schema_version,
        "telemetry_schema_version": record.telemetry_schema_version,
        "candidate_ids": list(record.candidate_ids),
        "selected_id": record.selected_id,
        "probabilities": dict(record.probabilities),
        "runner_up_id": record.runner_up_id,
        "margin": record.margin,
        "confidence": record.confidence,
        "latency_ms": record.latency_ms,
        "cost_usd": record.cost_usd,
        "trace_id": record.trace_id,
        "operation_id": record.operation_id,
        "campaign_id": record.campaign_id,
        "turn_id": record.turn_id,
        "frame_id": record.frame_id,
        "state_revision": record.state_revision,
        "mode": record.mode,
        "policy_directive": record.policy_directive,
        "verified": record.verified,
        "revalidation_error": record.revalidation_error,
    }


def _record_to_columns(record: DecisionRecord) -> dict[str, Any]:
    return {
        "trace_id": record.trace_id,
        "operation_id": record.operation_id,
        "decision_class": record.decision_class,
        "question_id": record.question_id,
        "question_kind": record.question_kind,
        "provider": record.provider,
        "model": record.model,
        "model_version": record.model_version,
        "candidate_schema_version": record.candidate_schema_version,
        "frame_schema_version": record.frame_schema_version,
        "policy_schema_version": record.policy_schema_version,
        "telemetry_schema_version": record.telemetry_schema_version,
        "candidate_ids": list(record.candidate_ids),
        "selected_id": record.selected_id,
        "probabilities": dict(record.probabilities),
        "runner_up_id": record.runner_up_id,
        "margin": record.margin,
        "confidence": record.confidence,
        "latency_ms": record.latency_ms,
        "cost_usd": record.cost_usd,
        "campaign_id": _as_uuid_or_none(record.campaign_id),
        "turn_id": _as_uuid_or_none(record.turn_id),
        "frame_id": record.frame_id or None,
        "state_revision": record.state_revision or None,
        "mode": record.mode,
        "policy_directive": record.policy_directive,
        "verified": record.verified,
        "revalidation_error": record.revalidation_error,
        "ground_truth_id": record.ground_truth_id,
        "correction_source": record.correction_source,
        "correction_indicates_wrong": record.correction_indicates_wrong,
        "corrected_to": record.corrected_to,
    }


def _as_uuid_or_none(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _telemetry_write(session_factory: Any, write: Callable[[Any], Any]) -> Any:
    """Own a short telemetry transaction on an independent session.

    Accepting a factory — not a Session — is intentional: instrumentation
    can neither commit nor poison a caller's gameplay transaction. Mirrors
    the observability service convention.
    """
    from sqlalchemy.orm import Session as _Session

    if isinstance(session_factory, _Session):
        raise TypeError(
            "telemetry writes require a dedicated session factory, not a Session"
        )
    with session_factory() as db:
        try:
            result = write(db)
            db.commit()
            db.refresh(result)
            db.expunge(result)
            return result
        except Exception:
            db.rollback()
            raise


def persist_record(session_factory: Any, record: DecisionRecord) -> Any:
    """Persist one decision record; raises on observability failure.

    Prefer :func:`record_fail_soft` from gameplay paths so a telemetry
    outage never mutates or duplicates gameplay.
    """
    from models.reliability import DecisionTelemetry

    def _write(db: Any) -> Any:
        row = DecisionTelemetry(**_record_to_columns(record))
        db.add(row)
        return row

    return _telemetry_write(session_factory, _write)


def record_fail_soft(session_factory: Any, record: DecisionRecord) -> Any | None:
    """Persist one decision record without ever breaking gameplay.

    Returns the detached row on success and ``None`` on any observability
    failure (including a missing factory). Never raises.
    """
    if session_factory is None:
        return None
    try:
        return persist_record(session_factory, record)
    except Exception as exc:
        logger.warning("decision telemetry write dropped: %s", exc)
        return None
