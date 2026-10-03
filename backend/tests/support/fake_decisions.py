"""Deterministic fake adapter for unit/E2E fixtures (issue #380).

Answers from a caller-supplied script without any network call, so tests
exercise the full runtime path — validation, normalization, unknown-choice
rejection, trace metadata — repeatably and without paid calls.
"""

from __future__ import annotations

from typing import Any

from app.decisions.adapters.base import (
    DecisionAdapter,
    checked_probability,
    checked_score,
)
from app.decisions.contracts import (
    ChoiceQuestion,
    ChoiceResult,
    DecisionRequest,
    NoulQuestion,
    NoulResult,
    ScoreQuestion,
    ScoreResult,
)
from app.decisions.errors import DecisionError

FAKE_ADAPTER_NAME = "fake-decision"
FAKE_MODEL_NAME = "fake-decision-model-v1"


class FakeDecisionAdapter(DecisionAdapter):
    """Scripted answers keyed by question ID.

    ``answers`` maps question IDs to scripted outputs: a candidate ID
    string for choice questions, a probability float for noul questions,
    or a score float for score questions. An unscripted question raises
    :exc:`DecisionError` (malformed) instead of inventing output.
    """

    name = FAKE_ADAPTER_NAME

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self._answers = dict(answers or {})
        self.calls: list[dict[str, Any]] = []

    def require_config(self, model: str | None = None) -> None:
        return None

    def default_model(self) -> str:
        return FAKE_MODEL_NAME

    def build_payload(self, request: DecisionRequest, *, model: str) -> dict[str, Any]:
        return {
            "state": request.state,
            "model": model,
            "questions": {q.question_id: q.kind for q in request.questions},
        }

    def execute(self, request: DecisionRequest, *, model: str, timeout: float) -> Any:
        """Answer offline: return native-shaped scripted data for parsing."""
        payload = self.build_payload(request, model=model)
        self.calls.append(
            {
                "questions": sorted(payload["questions"]),
                "state": request.state,
            }
        )
        return {
            "answers": {
                question_id: self._answers[question_id]
                for question_id in payload["questions"]
                if question_id in self._answers
            }
        }

    def parse_response(
        self, data: Any, request: DecisionRequest
    ) -> tuple[dict[str, Any], str | None, dict]:
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise DecisionError(
                f"Provider {self.name} returned a response without answers",
                provider=self.name,
                kind="malformed",
            )
        answers = data["answers"]
        results: dict[str, Any] = {}
        for question in request.questions:
            if question.question_id not in answers:
                raise DecisionError(
                    f"fake-decision has no scripted answer for question "
                    f"{question.question_id!r}",
                    provider=self.name,
                    kind="malformed",
                )
            scripted = answers[question.question_id]
            if isinstance(question, ChoiceQuestion):
                legal = {candidate.id for candidate in question.candidates}
                if not isinstance(scripted, str) or scripted not in legal:
                    raise DecisionError(
                        f"fake-decision scripted unknown candidate {scripted!r} "
                        f"for question {question.question_id!r}",
                        provider=self.name,
                        kind="malformed",
                    )
                results[question.question_id] = ChoiceResult(
                    question_id=question.question_id,
                    selected_id=scripted,
                    probabilities={
                        candidate.id: 1.0 if candidate.id == scripted else 0.0
                        for candidate in question.candidates
                    },
                    confidence=1.0,
                )
            elif isinstance(question, NoulQuestion):
                results[question.question_id] = NoulResult(
                    question_id=question.question_id,
                    probability=checked_probability(
                        scripted,
                        what="scripted noul probability",
                        question_id=question.question_id,
                        provider=self.name,
                    ),
                )
            elif isinstance(question, ScoreQuestion):
                results[question.question_id] = ScoreResult(
                    question_id=question.question_id,
                    score=checked_score(
                        scripted,
                        levels=len(question.levels),
                        question_id=question.question_id,
                        provider=self.name,
                    ),
                    probabilities={},
                    confidence=1.0,
                )
        return results, FAKE_MODEL_NAME, {}
