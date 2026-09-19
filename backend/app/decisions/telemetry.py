"""Decision telemetry, shadow execution, replay, and calibration — issue #383.

Every bounded semantic decision can be measured, replayed, calibrated, and
compared against later authoritative outcomes before and after it is allowed
to affect gameplay.

Responsibility split (never inverted):

- Deterministic code enumerates candidates, owns authorization/legality, and
  performs revalidation. Decision models choose only among code-supplied
  candidate IDs. Model confidence is evidence for the execution policy —
  never authorization, and never treated as calibrated until measured
  per decision role via :func:`calibration_summary`.
- Telemetry is observability only: persistence failures never mutate or
  duplicate gameplay. All writes go through short independent transactions
  (a session factory, never a gameplay ``Session``) and fail soft.
- Replay is read-only: :func:`replay_frame` / :func:`replay_batch` take no
  session and perform no writes; they re-evaluate stored synthetic/test
  frames against a supplied decision function.
- Calibration is per decision role (``decision_class``). There is
  deliberately no global accuracy/confidence aggregate.
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
from typing import Any, Callable, Iterable, Mapping

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

# Confidence bucket count for calibration summaries (deciles by default).
DEFAULT_CALIBRATION_BUCKETS = 10


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


def record_ground_truth(
    session_factory: Any,
    row_id: Any,
    ground_truth_id: str,
    *,
    strict: bool = False,
) -> Any | None:
    """Stamp the definitive authoritative outcome on a telemetry row.

    Distinct from :func:`record_correction`: ground truth is the
    authoritative answer (e.g. a later human-visible resolution or a
    deterministic oracle); correction signals are delayed wrongness
    evidence only. Returns the row, or ``None`` fail-soft unless
    ``strict`` is set.
    """
    from models.reliability import DecisionTelemetry

    def _write(db: Any) -> Any:
        row = db.get(DecisionTelemetry, row_id)
        if row is None:
            raise LookupError(f"decision telemetry row {row_id} not found")
        row.ground_truth_id = ground_truth_id
        return row

    try:
        return _telemetry_write(session_factory, _write)
    except Exception as exc:
        if strict:
            raise
        logger.warning("decision ground-truth write dropped: %s", exc)
        return None


def record_correction(
    session_factory: Any,
    row_id: Any,
    *,
    source: str,
    indicates_wrong: bool,
    corrected_to: str | None = None,
    strict: bool = False,
) -> Any | None:
    """Record a downstream correction/repair/validator signal.

    Delayed wrongness evidence only — never ground truth. ``source`` names
    the deterministic signal (e.g. ``"validator_rejection"``,
    ``"repair_loop"``, ``"player_correction"``). ``indicates_wrong`` says
    whether the signal evidences that the active decision was wrong, and
    ``corrected_to`` optionally names the replacement candidate. Returns
    the row, or ``None`` fail-soft unless ``strict`` is set.
    """
    from models.reliability import DecisionTelemetry

    if not isinstance(source, str) or not source.strip():
        raise DecisionError("correction signal is missing a source", kind="malformed")
    if not isinstance(indicates_wrong, bool):
        raise DecisionError(
            f"correction indicates_wrong {indicates_wrong!r} is not a boolean",
            kind="malformed",
        )

    def _write(db: Any) -> Any:
        row = db.get(DecisionTelemetry, row_id)
        if row is None:
            raise LookupError(f"decision telemetry row {row_id} not found")
        row.correction_source = source
        row.correction_indicates_wrong = indicates_wrong
        row.corrected_to = corrected_to
        return row

    try:
        return _telemetry_write(session_factory, _write)
    except Exception as exc:
        if strict:
            raise
        logger.warning("decision correction write dropped: %s", exc)
        return None


def shadow_decide(
    frame: DecisionFrame,
    decide_fn: Callable[[DecisionFrame], tuple[ChoiceResult, PolicyVerdict]],
) -> tuple[ChoiceResult, PolicyVerdict] | None:
    """Evaluate a decision in shadow mode: no gameplay effect, never raises.

    ``decide_fn`` runs the full decision path (adapter call + policy) and
    its outcome is returned for telemetry comparison, but the caller must
    continue down the existing authoritative path unchanged. Any
    decision-plane failure returns ``None`` instead of raising.
    """
    try:
        return decide_fn(frame)
    except Exception as exc:
        logger.warning("shadow decision dropped: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Offline replay (read-only)
# ---------------------------------------------------------------------------


def serialize_frame(frame: DecisionFrame) -> dict[str, Any]:
    """Snapshot a synthetic/test frame for offline replay storage.

    Captures the code-enumerated candidate set (IDs, labels, risk,
    reversibility, sources) plus the authorized state snapshot so a later
    model/policy version can re-decide the identical question. Intended
    for synthetic/test frames; never a gameplay mutation path.
    """
    return {
        "telemetry_schema_version": TELEMETRY_SCHEMA_VERSION,
        "decision_class": frame.decision_class,
        "question_id": frame.question_id,
        "instructions": frame.instructions,
        "state": frame.state,
        "state_revision": frame.state_revision,
        "frame_id": frame.frame_id,
        "schema_version": frame.schema_version,
        "candidates": [
            {
                "id": c.id,
                "label": c.label,
                "source": c.source,
                "source_ref": c.source_ref,
                "payload_ref": c.payload_ref,
                "debug_hint": c.debug_hint,
                "risk": c.risk,
                "reversible": c.reversible,
                "schema_version": c.schema_version,
            }
            for c in frame.candidates
        ],
    }


def deserialize_frame(data: Mapping[str, Any]) -> DecisionFrame:
    """Rebuild a frame from a stored replay snapshot (no I/O, no mutation)."""
    from app.decisions.frames import CandidateRecord, DecisionFrame

    try:
        candidates = tuple(
            CandidateRecord(
                id=c["id"],
                label=c["label"],
                source=c["source"],
                source_ref=c.get("source_ref"),
                payload_ref=c.get("payload_ref"),
                debug_hint=c.get("debug_hint"),
                risk=c.get("risk", "standard"),
                reversible=c.get("reversible", True),
            )
            for c in data["candidates"]
        )
        return DecisionFrame(
            decision_class=data["decision_class"],
            question_id=data["question_id"],
            instructions=data["instructions"],
            state=data["state"],
            state_revision=data["state_revision"],
            candidates=candidates,
            frame_id=str(data.get("frame_id") or uuid.uuid4().hex),
        )
    except KeyError as error:
        raise DecisionError(
            f"replay snapshot is missing required key {error}",
            kind="malformed",
        ) from error


@dataclass(frozen=True)
class ReplayOutcome:
    """One read-only replay evaluation against a new model/policy version."""

    frame_id: str
    decision_class: str
    question_id: str
    selected_id: str
    probabilities: dict[str, float]
    confidence: float | None
    margin: float
    directive: str
    replay_model: str
    replay_policy_version: int


def replay_frame(
    stored: Mapping[str, Any],
    decide_fn: Callable[[DecisionFrame], tuple[ChoiceResult, PolicyVerdict, str]],
) -> ReplayOutcome:
    """Re-evaluate one stored frame against a new model/policy version.

    ``decide_fn`` receives the deserialized frame and returns
    ``(result, verdict, model_name)``; it must perform no gameplay writes
    (pass a scripted/offline decision function). This function itself takes
    no session and performs no writes — replay cannot reapply side effects.
    Raises :exc:`DecisionError` when the stored snapshot is malformed or
    the replay decision fails.
    """
    frame = deserialize_frame(stored)
    result, verdict, replay_model = decide_fn(frame)
    if result.question_id != frame.question_id:
        raise DecisionError(
            f"replay result {result.question_id!r} does not belong to replayed "
            f"question {frame.question_id!r}",
            kind="malformed",
        )
    _, margin = runner_up(dict(result.probabilities), result.selected_id)
    return ReplayOutcome(
        frame_id=frame.frame_id,
        decision_class=frame.decision_class,
        question_id=frame.question_id,
        selected_id=result.selected_id,
        probabilities=dict(result.probabilities),
        confidence=result.confidence,
        margin=margin,
        directive=verdict.directive,
        replay_model=replay_model,
        replay_policy_version=verdict.policy_version,
    )


def replay_batch(
    stored_frames: Iterable[Mapping[str, Any]],
    decide_fn: Callable[[DecisionFrame], tuple[ChoiceResult, PolicyVerdict, str]],
) -> list[ReplayOutcome]:
    """Re-evaluate stored frames read-only; one failure never stops the batch.

    Frames that fail to deserialize or re-decide are skipped with a warning
    (counted in the returned list only on success) so a single corrupt
    snapshot cannot wedge an eval run.
    """
    outcomes: list[ReplayOutcome] = []
    for stored in stored_frames:
        try:
            outcomes.append(replay_frame(stored, decide_fn))
        except Exception as exc:
            logger.warning("replay frame skipped: %s", exc)
    return outcomes


# ---------------------------------------------------------------------------
# Calibration / confusion summaries (per role, never global)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationBucket:
    """One confidence bucket within one decision role."""

    lower: float
    upper: float
    n: int
    mean_confidence: float | None
    accuracy: float | None
    direct_execute_error_rate: float | None
    unnecessary_escalation_rate: float | None
    correction_rate: float | None


def _bucket_index(confidence: float, bucket_count: int) -> int:
    clamped = min(max(float(confidence), 0.0), 1.0)
    index = int(clamped * bucket_count)
    return min(index, bucket_count - 1)


def _is_wrong(record: DecisionRecord) -> bool | None:
    """Delayed wrongness evidence for one record, or ``None`` if unknown.

    Definitive ground truth wins when present; otherwise a correction
    signal indicating wrong counts as wrong evidence. Records with neither
    carry no wrongness evidence either way.
    """
    if record.ground_truth_id is not None:
        return record.selected_id != record.ground_truth_id
    if record.correction_indicates_wrong is not None:
        return record.correction_indicates_wrong
    return None


def _is_right(record: DecisionRecord) -> bool | None:
    wrong = _is_wrong(record)
    return None if wrong is None else not wrong


def calibration_summary(
    records: Iterable[DecisionRecord],
    *,
    buckets: int = DEFAULT_CALIBRATION_BUCKETS,
) -> dict[str, list[CalibrationBucket]]:
    """Per-role calibration/confusion summary over confidence buckets.

    Keyed by decision role (``decision_class``) — roles are never collapsed
    into one global metric. Model confidence is reported alongside measured
    accuracy so callers can see whether it is calibrated for that role
    instead of assuming it.

    Per bucket:

    - ``accuracy``: fraction of records with known outcomes where the
      selection was right (ground truth preferred, correction evidence
      otherwise); ``None`` when no record in the bucket has an outcome.
    - ``direct_execute_error_rate``: fraction of ``direct_execute`` records
      with known outcomes that proved wrong; ``None`` when none qualify.
    - ``unnecessary_escalation_rate``: fraction of ``escalate`` records
      with known outcomes that proved right (the direct path would have
      succeeded); ``None`` when none qualify.
    - ``correction_rate``: fraction of records carrying any correction
      signal, measuring downstream repair pressure per role.
    """
    if (
        isinstance(buckets, bool)
        or not isinstance(buckets, int)
        or buckets < 1
    ):
        raise DecisionError(
            f"calibration bucket count {buckets!r} must be a positive integer",
            kind="malformed",
        )
    by_role: dict[str, list[DecisionRecord]] = {}
    for record in records:
        by_role.setdefault(record.decision_class, []).append(record)
    summary: dict[str, list[CalibrationBucket]] = {}
    for role in sorted(by_role):
        role_records = by_role[role]
        bucketed: list[list[DecisionRecord]] = [[] for _ in range(buckets)]
        for record in role_records:
            if record.confidence is None:
                continue
            bucketed[_bucket_index(record.confidence, buckets)].append(record)
        role_summary: list[CalibrationBucket] = []
        for index, members in enumerate(bucketed):
            lower = index / buckets
            upper = (index + 1) / buckets
            if not members:
                role_summary.append(
                    CalibrationBucket(
                        lower=lower,
                        upper=upper,
                        n=0,
                        mean_confidence=None,
                        accuracy=None,
                        direct_execute_error_rate=None,
                        unnecessary_escalation_rate=None,
                        correction_rate=None,
                    )
                )
                continue
            mean_confidence = sum(float(r.confidence) for r in members) / len(members)
            known = [(r, _is_wrong(r)) for r in members]
            known = [(r, w) for r, w in known if w is not None]
            accuracy = (
                sum(1 for _, w in known if not w) / len(known) if known else None
            )
            direct = [
                w
                for r, w in known
                if r.policy_directive == "direct_execute"
            ]
            direct_error = (
                sum(1 for w in direct if w) / len(direct) if direct else None
            )
            escalated = [
                w
                for r, w in known
                if r.policy_directive == "escalate"
            ]
            unnecessary = (
                sum(1 for w in escalated if not w) / len(escalated)
                if escalated
                else None
            )
            corrected = sum(1 for r in members if r.correction_source is not None)
            role_summary.append(
                CalibrationBucket(
                    lower=lower,
                    upper=upper,
                    n=len(members),
                    mean_confidence=mean_confidence,
                    accuracy=accuracy,
                    direct_execute_error_rate=direct_error,
                    unnecessary_escalation_rate=unnecessary,
                    correction_rate=corrected / len(members),
                )
            )
        summary[role] = role_summary
    return summary
