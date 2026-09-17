"""TypeSafe Jev adapter — reference implementation (issue #380).

Jev is the first production adapter, not the architecture: gameplay code
never imports this module. Wire shape follows the public System One API —

    POST {base_url}  (default ``https://api.typesafe.ai/v1/systemone``)
    {"state": ..., "model": "jev-latest",
     "questions": {qid: {"type": "choice"|"noul"|"score",
                         "instructions": ...,
                         "criteria": {candidate_id: description} | [levels]}}}

and answers come back keyed by question ID with per-option probabilities
plus ``confidence`` on choice/score answers.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlparse

import requests

from app.decisions.adapters.base import (
    DecisionAdapter,
    checked_probability,
    checked_score,
    checked_score_key,
)
from app.decisions.config import RETRYABLE_STATUS_CODES, api_key, base_url, default_model
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


class JevAdapter(DecisionAdapter):
    name = "jev"

    def api_key(self) -> str:
        return api_key().strip()

    def env_model(self) -> str:
        return os.environ.get("JEV_MODEL", default_model()).strip()

    def base_url(self) -> str:
        return base_url().strip()

    def require_config(self, model: str | None = None) -> None:
        if not self.api_key():
            raise DecisionError(
                "TYPESAFE_API_KEY is not set", provider=self.name, kind="config"
            )
        if not (model or self.env_model()):
            raise DecisionError(
                "JEV_MODEL is not set", provider=self.name, kind="config"
            )
        parsed = urlparse(self.base_url())
        try:
            parsed.port
        except ValueError as error:
            raise DecisionError(
                f"TYPESAFE_BASE_URL has an invalid port: {self.base_url()!r}",
                provider=self.name,
                kind="config",
            ) from error
        if not parsed.hostname:
            raise DecisionError(
                f"TYPESAFE_BASE_URL is not an absolute HTTP(S) URL: "
                f"{self.base_url()!r}",
                provider=self.name,
                kind="config",
            )
        # Bearer credentials must never travel over plaintext on the wire;
        # plain HTTP is allowed only for loopback development endpoints.
        # Every other scheme fails here, before any AI run starts.
        if parsed.scheme == "http":
            if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise DecisionError(
                    f"TYPESAFE_BASE_URL must be HTTPS for non-loopback endpoints: "
                    f"{self.base_url()!r}",
                    provider=self.name,
                    kind="config",
                )
        elif parsed.scheme != "https":
            raise DecisionError(
                f"TYPESAFE_BASE_URL must use HTTP(S): {self.base_url()!r}",
                provider=self.name,
                kind="config",
            )

    def default_model(self) -> str:
        return self.env_model()

    def build_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key()}",
            "Content-Type": "application/json",
        }

    def execute(self, request: DecisionRequest, *, model: str, timeout: float) -> Any:
        """POST one System One call and return the decoded response body."""
        self.require_config(model)
        payload = self.build_payload(request, model=model)
        try:
            response = requests.post(
                self.base_url(),
                headers=self.build_headers(),
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
        except Exception as error:
            raise self.classify_error(error) from error
        try:
            return response.json()
        except ValueError as error:
            raise DecisionError(
                f"Provider {self.name} returned invalid JSON",
                provider=self.name,
                kind="malformed",
                original=error,
            ) from error

    def build_payload(self, request: DecisionRequest, *, model: str) -> dict[str, Any]:
        questions: dict[str, Any] = {}
        for question in request.questions:
            if isinstance(question, ChoiceQuestion):
                questions[question.question_id] = {
                    "type": "choice",
                    "instructions": question.instructions,
                    "criteria": {
                        candidate.id: candidate.description
                        for candidate in question.candidates
                    },
                }
            elif isinstance(question, NoulQuestion):
                questions[question.question_id] = {
                    "type": "noul",
                    "instructions": question.instructions,
                }
            elif isinstance(question, ScoreQuestion):
                questions[question.question_id] = {
                    "type": "score",
                    "instructions": question.instructions,
                    "criteria": list(question.levels),
                }
            else:  # pragma: no cover - runtime validates first
                raise DecisionError(
                    f"unsupported question type: {type(question).__name__}",
                    provider=self.name,
                    kind="unsupported_feature",
                )
        return {"state": request.state, "model": model, "questions": questions}

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
        by_id = {q.question_id: q for q in request.questions}
        results: dict[str, Any] = {}
        for question_id, question in by_id.items():
            if question_id not in answers:
                raise DecisionError(
                    f"Provider {self.name} omitted answer for question {question_id!r}",
                    provider=self.name,
                    kind="malformed",
                )
            raw = answers[question_id]
            if not isinstance(raw, dict):
                raise DecisionError(
                    f"Provider {self.name} returned a non-object answer "
                    f"for question {question_id!r}",
                    provider=self.name,
                    kind="malformed",
                )
            if isinstance(question, ChoiceQuestion):
                results[question_id] = self._parse_choice(question, raw)
            elif isinstance(question, NoulQuestion):
                results[question_id] = self._parse_noul(question, raw)
            elif isinstance(question, ScoreQuestion):
                results[question_id] = self._parse_score(question, raw)
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise DecisionError(
                f"Provider {self.name} returned missing or malformed usage",
                provider=self.name,
                kind="malformed",
            )
        for field in ("input_tokens", "output_tokens"):
            value = usage.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise DecisionError(
                    f"Provider {self.name} returned invalid {field} {value!r}",
                    provider=self.name,
                    kind="malformed",
                )
        model = data.get("model")
        if not isinstance(model, str) or not model.strip():
            raise DecisionError(
                f"Provider {self.name} returned missing or invalid model identity",
                provider=self.name,
                kind="malformed",
            )
        return results, model, usage

    def _require_type(self, raw: dict, expected: str, question_id: str) -> None:
        if raw.get("type") != expected:
            raise DecisionError(
                f"Provider {self.name} returned type {raw.get('type')!r} "
                f"for {expected} question {question_id!r}",
                provider=self.name,
                kind="malformed",
            )

    @staticmethod
    def _require_unit_sum(
        normalized: dict[str, float], *, what: str, question_id: str, provider: str
    ) -> None:
        # The provider contract defines these maps as probabilities summing
        # to 1; only a small floating-point tolerance is allowed, so any
        # materially non-unit map is malformed calibration data.
        total = sum(normalized.values())
        if abs(total - 1.0) > 0.01 + 1e-9:
            raise DecisionError(
                f"Provider {provider} returned a {what} distribution summing to "
                f"{total!r} for question {question_id!r}",
                provider=provider,
                kind="malformed",
            )

    def _parse_choice(self, question: ChoiceQuestion, raw: dict) -> ChoiceResult:
        self._require_type(raw, "choice", question.question_id)
        selected = raw.get("choice")
        legal = {candidate.id for candidate in question.candidates}
        if not isinstance(selected, str) or selected not in legal:
            raise DecisionError(
                f"Provider {self.name} returned unknown candidate "
                f"{selected!r} for question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        probabilities = raw.get("probabilities")
        if not isinstance(probabilities, dict) or set(probabilities) != legal:
            raise DecisionError(
                f"Provider {self.name} returned an incomplete distribution "
                f"for choice question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        normalized = {
            candidate_id: checked_probability(
                value,
                what=f"probability for candidate {candidate_id!r}",
                question_id=question.question_id,
                provider=self.name,
            )
            for candidate_id, value in probabilities.items()
        }
        self._require_unit_sum(
            normalized,
            what="choice",
            question_id=question.question_id,
            provider=self.name,
        )
        # The scalar answer must agree with its distribution: the selected
        # choice is the maximum-probability option, within 2-decimal
        # rounding plus epsilon (ties round either way).
        selected_probability = normalized[selected]
        if max(normalized.values()) - selected_probability > 0.01 + 1e-9:
            raise DecisionError(
                f"Provider {self.name} returned choice {selected!r} "
                f"contradicting its distribution "
                f"for question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        confidence = raw.get("confidence")
        if confidence is None:
            raise DecisionError(
                f"Provider {self.name} omitted confidence "
                f"for choice question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        return ChoiceResult(
            question_id=question.question_id,
            selected_id=selected,
            probabilities=normalized,
            confidence=checked_probability(
                confidence,
                what="confidence",
                question_id=question.question_id,
                provider=self.name,
            ),
        )

    def _parse_noul(self, question: NoulQuestion, raw: dict) -> NoulResult:
        self._require_type(raw, "noul", question.question_id)
        return NoulResult(
            question_id=question.question_id,
            probability=checked_probability(
                raw.get("noul", raw.get("probability")),
                what="noul probability",
                question_id=question.question_id,
                provider=self.name,
            ),
        )

    def _parse_score(self, question: ScoreQuestion, raw: dict) -> ScoreResult:
        self._require_type(raw, "score", question.question_id)
        level_count = len(question.levels)
        expected_keys = {str(index) for index in range(level_count)}
        # The legend maps numeric level keys back to their descriptions; it
        # must exactly echo the requested ordered levels, otherwise the
        # numeric score would be interpreted against the wrong rubric.
        expected_legend = {str(index): level for index, level in enumerate(question.levels)}
        if not isinstance(raw.get("legend"), dict) or raw["legend"] != expected_legend:
            raise DecisionError(
                f"Provider {self.name} returned a missing or mismatched legend "
                f"for score question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        probabilities = raw.get("probabilities")
        if not isinstance(probabilities, dict):
            raise DecisionError(
                f"Provider {self.name} returned malformed probabilities "
                f"for question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        normalized = {
            checked_score_key(
                key,
                levels=level_count,
                question_id=question.question_id,
                provider=self.name,
            ): checked_probability(
                value,
                what=f"probability for level {key!r}",
                question_id=question.question_id,
                provider=self.name,
            )
            for key, value in probabilities.items()
        }
        if set(normalized) != expected_keys:
            raise DecisionError(
                f"Provider {self.name} returned an incomplete distribution "
                f"for score question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        self._require_unit_sum(
            normalized,
            what="score",
            question_id=question.question_id,
            provider=self.name,
        )
        confidence = raw.get("confidence")
        if confidence is None:
            raise DecisionError(
                f"Provider {self.name} omitted confidence "
                f"for score question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        score = checked_score(
            raw.get("score"),
            levels=level_count,
            question_id=question.question_id,
            provider=self.name,
        )
        # The scalar score must agree with its distribution: it is the
        # probability-weighted level mean, within rounding tolerance
        # (2-decimal probabilities over at most 10 levels).
        weighted_mean = sum(
            int(level) * probability for level, probability in normalized.items()
        )
        if abs(score - weighted_mean) > 0.05 + 1e-9:
            raise DecisionError(
                f"Provider {self.name} returned score {score!r} "
                f"contradicting its distribution "
                f"for question {question.question_id!r}",
                provider=self.name,
                kind="malformed",
            )
        return ScoreResult(
            question_id=question.question_id,
            score=score,
            probabilities=normalized,
            confidence=checked_probability(
                confidence,
                what="confidence",
                question_id=question.question_id,
                provider=self.name,
            ),
        )

    def classify_error(self, error: Exception) -> DecisionError:
        if isinstance(error, DecisionError):
            return error
        if isinstance(error, requests.Timeout):
            return DecisionError(
                repr(error), provider=self.name, retryable=True, kind="timeout", original=error
            )
        if isinstance(error, requests.ConnectionError):
            return DecisionError(
                repr(error),
                provider=self.name,
                retryable=True,
                kind="connection",
                original=error,
            )
        if isinstance(error, requests.HTTPError):
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 401:
                return DecisionError(
                    repr(error),
                    provider=self.name,
                    status_code=status_code,
                    retryable=False,
                    kind="config",
                    original=error,
                )
            if status_code == 422:
                return DecisionError(
                    repr(error),
                    provider=self.name,
                    status_code=status_code,
                    retryable=False,
                    kind="malformed",
                    original=error,
                )
            retryable = status_code in RETRYABLE_STATUS_CODES or (
                status_code is not None and status_code >= 500
            )
            return DecisionError(
                repr(error),
                provider=self.name,
                status_code=status_code,
                retryable=retryable,
                kind="http",
                original=error,
            )
        return DecisionError(
            repr(error), provider=self.name, retryable=False, kind="http", original=error
        )
