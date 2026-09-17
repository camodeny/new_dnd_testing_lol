"""Provider-neutral bounded decision contracts (issue #380).

Gameplay code submits typed semantic questions against an already-authorized
state payload and receives typed answers. A decision answer is never
permission to mutate state; callers still validate and execute through
authoritative domain services.

Question kinds mirror the reference adapter (TypeSafe Jev): ``choice``,
``noul`` (boolean judgment), and ``score`` (ordered rubric).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DecisionCandidate:
    """One legal answer for a ``choice`` question.

    The candidate set is owned by deterministic caller code. Adapters must
    never return an ID outside it; the runtime rejects such responses.
    """

    id: str
    description: str | None = None


@dataclass(frozen=True)
class ChoiceQuestion:
    question_id: str
    instructions: str
    candidates: tuple[DecisionCandidate, ...] = ()

    @property
    def kind(self) -> str:
        return "choice"


@dataclass(frozen=True)
class NoulQuestion:
    question_id: str
    instructions: str

    @property
    def kind(self) -> str:
        return "noul"


@dataclass(frozen=True)
class ScoreQuestion:
    question_id: str
    instructions: str
    levels: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return "score"


DecisionQuestion = ChoiceQuestion | NoulQuestion | ScoreQuestion


@dataclass(frozen=True)
class DecisionRequest:
    """One bounded decision call: N independent questions, one state payload.

    ``state`` is passed through verbatim. The caller must apply
    audience/visibility authorization before constructing it; the runtime
    never expands it.
    """

    questions: tuple[DecisionQuestion, ...]
    state: Any
    model: str | None = None
    timeout_seconds: float | None = None
    max_attempts: int | None = None


@dataclass(frozen=True)
class ChoiceResult:
    question_id: str
    selected_id: str
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None


@dataclass(frozen=True)
class NoulResult:
    question_id: str
    probability: float = 0.0


@dataclass(frozen=True)
class ScoreResult:
    question_id: str
    score: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None


DecisionResult = ChoiceResult | NoulResult | ScoreResult


@dataclass(frozen=True)
class DecisionResponse:
    """Typed results keyed to the requested question IDs, plus trace data."""

    results: dict[str, DecisionResult]
    provider: str
    model: str | None
    latency_ms: int
    trace_id: str | None
    operation_id: str | None = None
    usage: dict = field(default_factory=dict)
