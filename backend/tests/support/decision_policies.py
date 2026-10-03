"""Example decision-class policies for bounded-decision unit tests."""
from __future__ import annotations

from app.decisions.policy import (
    ESCALATE,
    PRIMER_ADVISORY,
    DecisionClassPolicy,
    register_policy,
)


def register_example_policies() -> None:
    """Register an aggressive and a conservative example class (idempotent)."""
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
