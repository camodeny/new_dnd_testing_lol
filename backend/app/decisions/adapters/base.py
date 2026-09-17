"""Abstract boundary every decision adapter implements.

Branching on decision providers lives here and in the runtime — never in
gameplay code, which depends only on ``contracts`` and the service.
"""

from __future__ import annotations

import math
from typing import Any

from app.decisions.contracts import DecisionRequest
from app.decisions.errors import DecisionError


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def checked_probability(
    value: Any, *, what: str, question_id: str, provider: str
) -> float:
    """Validate a [0, 1] probability/confidence value or raise malformed."""
    if not _finite_number(value) or not 0.0 <= float(value) <= 1.0:
        raise DecisionError(
            f"Provider {provider} returned out-of-range {what} {value!r} "
            f"for question {question_id!r}",
            provider=provider,
            kind="malformed",
        )
    return float(value)


def checked_score(
    value: Any, *, levels: int, question_id: str, provider: str
) -> float:
    """Validate a rubric score in [0, levels - 1] or raise malformed."""
    if not _finite_number(value) or not 0.0 <= float(value) <= levels - 1:
        raise DecisionError(
            f"Provider {provider} returned out-of-range score {value!r} "
            f"for question {question_id!r}",
            provider=provider,
            kind="malformed",
        )
    return float(value)


def checked_score_key(key: Any, *, levels: int, question_id: str, provider: str) -> str:
    """Validate a score-distribution key against the declared levels."""
    index = None
    if isinstance(key, int) and not isinstance(key, bool):
        index = key
    elif isinstance(key, str):
        try:
            index = int(key.strip())
        except ValueError:
            index = None
        if index is not None and str(index) != key.strip():
            index = None
    if index is None or not 0 <= index < levels:
        raise DecisionError(
            f"Provider {provider} returned an undeclared score level {key!r} "
            f"for question {question_id!r}",
            provider=provider,
            kind="malformed",
        )
    return str(index)


class DecisionAdapter:
    """Provider-neutral decision adapter interface."""

    name = "base"

    def require_config(self, model: str | None = None) -> None:
        raise NotImplementedError

    def default_model(self) -> str:
        """Adapter-owned default model so telemetry names the real identity."""
        raise NotImplementedError

    def capabilities(self) -> dict[str, bool]:
        return {"choice": True, "noul": True, "score": True}

    def execute(self, request: DecisionRequest, *, model: str, timeout: float) -> Any:
        """Fetch native provider response data for one attempt.

        Transport lives behind this boundary: HTTP adapters POST here,
        offline adapters answer here. Anything the runtime needs to send
        a request (URLs, headers, credentials) stays adapter-private, so
        gameplay code and the runtime never branch on provider specifics.
        Raises :exc:`DecisionError` on transport/config/decode failures.
        """
        raise NotImplementedError

    def build_payload(self, request: DecisionRequest, *, model: str) -> dict[str, Any]:
        raise NotImplementedError

    def parse_response(
        self, data: Any, request: DecisionRequest
    ) -> tuple[dict[str, Any], str | None, dict]:
        """Normalize a native response into ``(results, model, usage)``.

        Raises :exc:`DecisionError` with kind ``malformed`` when the
        response is not a well-formed answer set — including a ``choice``
        ID outside the caller-supplied candidate set.
        """
        raise NotImplementedError

    def classify_error(self, error: Exception) -> DecisionError:
        if isinstance(error, DecisionError):
            return error
        return DecisionError(
            repr(error), provider=self.name, retryable=False, kind="http", original=error
        )
