"""Decision-class execution policy — issue #381.

Maps a calibrated model selection to one execution directive:

- ``direct_execute`` — code may execute the revalidated candidate directly.
- ``primer_advisory`` — the selection is advisory input to a deterministic
  primer/confirmation step, not a direct execution.
- ``escalate`` — defer to the open-ended AI DM path / human-visible review.

Inputs are calibrated probability/confidence, the alternative margin
(top-1 minus top-2 probability), candidate reversibility, deterministic
verification outcome, and the candidate's consequence/risk class. Confidence
is evidence, never authorization: even a confident selection escalates when
the margin is a near-tie, the candidate is high-risk or irreversible beyond
its class allowance, or deterministic verification fails.

Thresholds are configured per decision class. There is deliberately no
single global threshold: each class carries its own policy, including its
own near-tie behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.decisions.errors import DecisionError
from app.decisions.frames import (
    RISK_ORDER,
    CandidateRecord,
    DecisionFrame,
    frame_trace,
    is_escape_id,
    resolve_candidate,
)

POLICY_SCHEMA_VERSION = 1

Directive = Literal["direct_execute", "primer_advisory", "escalate"]
NearTieBehavior = Literal["escalate", "primer_advisory"]

DIRECT_EXECUTE: Directive = "direct_execute"
PRIMER_ADVISORY: Directive = "primer_advisory"
ESCALATE: Directive = "escalate"


def _risk_rank(risk: str) -> int:
    try:
        return RISK_ORDER.index(risk)
    except ValueError as error:
        raise DecisionError(f"unknown risk class {risk!r}", kind="malformed") from error


@dataclass(frozen=True)
class DecisionClassPolicy:
    """Per-decision-class execution thresholds (schema v1)."""

    decision_class: str
    min_probability_direct: float
    min_confidence_direct: float
    min_margin_direct: float
    near_tie_margin: float
    near_tie_behavior: NearTieBehavior = ESCALATE
    allow_direct_when_irreversible: bool = False
    max_risk_for_direct: str = "standard"
    min_probability_primer: float = 0.0
    min_confidence_primer: float = 0.0
    schema_version: int = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.decision_class, str) or not self.decision_class.strip():
            raise DecisionError("policy is missing a decision class", kind="malformed")
        for attr in (
            "min_probability_direct",
            "min_confidence_direct",
            "min_margin_direct",
            "near_tie_margin",
            "min_probability_primer",
            "min_confidence_primer",
        ):
            value = getattr(self, attr)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0.0 <= float(value) <= 1.0
            ):
                raise DecisionError(
                    f"policy {self.decision_class!r} has out-of-range {attr} "
                    f"{value!r}; expected a number in [0, 1]",
                    kind="malformed",
                )
        if self.near_tie_behavior not in ("escalate", "primer_advisory"):
            raise DecisionError(
                f"policy {self.decision_class!r} has unknown near_tie_behavior "
                f"{self.near_tie_behavior!r}",
                kind="malformed",
            )
        if not isinstance(self.allow_direct_when_irreversible, bool):
            raise DecisionError(
                f"policy {self.decision_class!r} has a non-boolean "
                "allow_direct_when_irreversible flag",
                kind="malformed",
            )
        _risk_rank(self.max_risk_for_direct)
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise DecisionError(
                f"unsupported policy schema version {self.schema_version!r}",
                kind="unsupported_feature",
            )


@dataclass(frozen=True)
class PolicyVerdict:
    directive: Directive
    reason: str
    decision_class: str
    selected_id: str
    probability: float
    confidence: float
    margin: float
    policy_version: int = POLICY_SCHEMA_VERSION


# Canonical per-class registry. Classes tune their own posture; nothing here
# is a shared global threshold — adding a class means adding an entry.
POLICY_REGISTRY: dict[str, DecisionClassPolicy] = {}


def register_policy(policy: DecisionClassPolicy) -> DecisionClassPolicy:
    """Register (or replace) the policy for one decision class."""
    POLICY_REGISTRY[policy.decision_class] = policy
    return policy


def get_policy(decision_class: str) -> DecisionClassPolicy:
    """Look up the policy for a decision class or raise malformed."""
    try:
        return POLICY_REGISTRY[decision_class]
    except KeyError as error:
        raise DecisionError(
            f"no execution policy registered for decision class "
            f"{decision_class!r}",
            kind="malformed",
        ) from error


def _seed_builtin_policies() -> None:
    register_policy(
        DecisionClassPolicy(
            decision_class="skirmish_action",
            # Aggressive posture for low-stakes reversible tactics: modest
            # confidence + a clear margin executes directly.
            min_probability_direct=0.55,
            min_confidence_direct=0.5,
            min_margin_direct=0.10,
            near_tie_margin=0.10,
            near_tie_behavior=PRIMER_ADVISORY,
            allow_direct_when_irreversible=False,
            max_risk_for_direct="standard",
            min_probability_primer=0.35,
            min_confidence_primer=0.3,
        )
    )
    register_policy(
        DecisionClassPolicy(
            decision_class="campaign_consequence",
            # Conservative posture for story-consequential calls: high bar for
            # direct execution, near-ties escalate to the open-ended AI DM.
            min_probability_direct=0.85,
            min_confidence_direct=0.8,
            min_margin_direct=0.25,
            near_tie_margin=0.25,
            near_tie_behavior=ESCALATE,
            allow_direct_when_irreversible=False,
            max_risk_for_direct="low",
            min_probability_primer=0.5,
            min_confidence_primer=0.45,
        )
    )


_seed_builtin_policies()


def _checked_unit(value: float | None, *, what: str) -> float:
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise DecisionError(
            f"policy input {what} {value!r} is not a probability in [0, 1]",
            kind="malformed",
        )
    return float(value)


def alternative_margin(probabilities: dict[str, float], selected_id: str) -> float:
    """Top-1 minus top-2 probability over an already-validated distribution."""
    top = probabilities[selected_id]
    runner_up = max(
        (value for candidate_id, value in probabilities.items() if candidate_id != selected_id),
        default=0.0,
    )
    return max(0.0, top - runner_up)


# Same invariants as the adapter boundary: a choice distribution covers
# exactly the frame candidates and sums to 1 within 2-decimal rounding plus
# epsilon. Anything looser lets a sliced map inflate the margin.
DISTRIBUTION_SUM_TOLERANCE = 0.01
_DISTRIBUTION_EPSILON = 1e-9


def _checked_distribution(
    frame: DecisionFrame, probabilities: object
) -> dict[str, float]:
    """Validate a complete unit-sum distribution over the frame candidates.

    Raises malformed on missing/extra keys, non-probability values, maps
    that do not sum to 1, or a selection contradicting the maximum.
    """
    expected = {c.id for c in frame.candidates}
    if not isinstance(probabilities, dict) or set(probabilities) != expected:
        raise DecisionError(
            f"policy input distribution for question {frame.question_id!r} must "
            f"cover exactly the frame candidates; got {sorted(probabilities) if isinstance(probabilities, dict) else probabilities!r}",
            kind="malformed",
        )
    normalized = {
        candidate_id: _checked_unit(value, what=f"probability for candidate {candidate_id!r}")
        for candidate_id, value in probabilities.items()
    }
    total = sum(normalized.values())
    if abs(total - 1.0) > DISTRIBUTION_SUM_TOLERANCE + _DISTRIBUTION_EPSILON:
        raise DecisionError(
            f"policy input distribution for question {frame.question_id!r} "
            f"sums to {total!r}, not 1",
            kind="malformed",
        )
    return normalized


def evaluate_execution(
    frame: DecisionFrame,
    selected_id: str,
    probabilities: dict[str, float],
    confidence: float | None,
    *,
    policy: DecisionClassPolicy | None = None,
    verified: bool,
) -> PolicyVerdict:
    """Produce the execution directive for one calibrated selection.

    ``verified`` is the deterministic verification outcome owned by code
    (legality/authorization/idempotency/geometry/arithmetic checks) and has
    no default — callers must pass it explicitly. A ``False`` value always
    escalates, regardless of model confidence; omitting it is a TypeError,
    never an assumed success. Unknown candidate IDs raise malformed — the
    model selects only supplied candidates.
    """
    active = policy or get_policy(frame.decision_class)
    if active.decision_class != frame.decision_class:
        raise DecisionError(
            f"policy class {active.decision_class!r} does not match frame class "
            f"{frame.decision_class!r}",
            kind="malformed",
        )
    candidate: CandidateRecord = resolve_candidate(frame, selected_id)
    # The distribution is validated before any branch uses it — including the
    # escape branch — so a sliced or non-unit map can never inflate a margin.
    distribution = _checked_distribution(frame, probabilities)
    if (
        max(distribution.values()) - distribution[candidate.id]
        > DISTRIBUTION_SUM_TOLERANCE + _DISTRIBUTION_EPSILON
    ):
        raise DecisionError(
            f"policy input selection {candidate.id!r} contradicts its "
            f"distribution for question {frame.question_id!r}",
            kind="malformed",
        )
    if is_escape_id(candidate.id):
        return PolicyVerdict(
            directive=ESCALATE,
            reason=f"escape candidate {candidate.id} defers to the open-ended AI DM path",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=distribution[candidate.id],
            confidence=_checked_unit(confidence, what="confidence"),
            margin=alternative_margin(distribution, candidate.id),
        )
    probability = distribution[candidate.id]
    confidence_value = _checked_unit(confidence, what="confidence")
    margin = alternative_margin(distribution, candidate.id)

    # Truthiness is not verification: a truthy non-boolean such as "false"
    # must never read as a passed deterministic check.
    if not isinstance(verified, bool):
        raise DecisionError(
            f"policy input verified {verified!r} is not a boolean",
            kind="malformed",
        )
    if not verified:
        return PolicyVerdict(
            directive=ESCALATE,
            reason="deterministic verification failed; confidence is not authorization",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    if _risk_rank(candidate.risk) > _risk_rank(active.max_risk_for_direct):
        return PolicyVerdict(
            directive=ESCALATE,
            reason=f"risk class {candidate.risk!r} exceeds direct-execution allowance "
            f"{active.max_risk_for_direct!r} for {frame.decision_class!r}",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    if margin < active.near_tie_margin:
        return PolicyVerdict(
            directive=active.near_tie_behavior,
            reason=f"near-tie margin {margin:.3f} below {active.near_tie_margin:.3f}",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    direct_ok = (
        probability >= active.min_probability_direct
        and confidence_value >= active.min_confidence_direct
        and margin >= active.min_margin_direct
    )
    if direct_ok and (candidate.reversible or active.allow_direct_when_irreversible):
        return PolicyVerdict(
            directive=DIRECT_EXECUTE,
            reason="calibrated selection clears class thresholds and is executable",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    if direct_ok and not candidate.reversible:
        return PolicyVerdict(
            directive=PRIMER_ADVISORY,
            reason="irreversible candidate needs a primer/confirmation step",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    if (
        probability >= active.min_probability_primer
        and confidence_value >= active.min_confidence_primer
    ):
        return PolicyVerdict(
            directive=PRIMER_ADVISORY,
            reason="below direct thresholds; advisory primer only",
            decision_class=frame.decision_class,
            selected_id=candidate.id,
            probability=probability,
            confidence=confidence_value,
            margin=margin,
        )
    return PolicyVerdict(
        directive=ESCALATE,
        reason="below primer thresholds; escalate to the open-ended AI DM path",
        decision_class=frame.decision_class,
        selected_id=candidate.id,
        probability=probability,
        confidence=confidence_value,
        margin=margin,
    )


def policy_trace(frame: DecisionFrame, verdict: PolicyVerdict) -> dict[str, object]:
    """Trace metadata stamping candidate + frame + policy schema versions."""
    trace = frame_trace(frame, policy_version=POLICY_SCHEMA_VERSION)
    trace.update(
        {
            "directive": verdict.directive,
            "reason": verdict.reason,
            "selected_id": verdict.selected_id,
            "probability": verdict.probability,
            "confidence": verdict.confidence,
            "margin": verdict.margin,
        }
    )
    return trace
