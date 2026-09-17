"""Bounded decision runtime tests — issue #380 verification list."""

from __future__ import annotations

import requests

import pytest

from app.decisions.adapters.fake import FAKE_MODEL_NAME, FakeDecisionAdapter
from app.decisions.adapters.jev import JevAdapter
from app.decisions.contracts import (
    ChoiceQuestion,
    DecisionCandidate,
    DecisionRequest,
    NoulQuestion,
    ScoreQuestion,
)
from app.decisions.errors import DecisionError
from app.decisions.runtime import DecisionService

# Ordered levels of the shared "anger" score fixture, and the exact legend
# plus a unit-sum distribution a well-formed Jev answer carries for them.
ANGER_LEGEND = {"0": "calm", "1": "annoyed", "2": "furious"}
ANGER_PROBABILITIES = {"0": 0.1, "1": 0.8, "2": 0.1}


def _request(**overrides):
    questions = overrides.pop(
        "questions",
        (
            ChoiceQuestion(
                question_id="route",
                instructions="Which lane handles this?",
                candidates=(
                    DecisionCandidate(id="billing", description="Payment issues"),
                    DecisionCandidate(id="technical", description="Bugs"),
                ),
            ),
            NoulQuestion(question_id="urgent", instructions="Is this urgent?"),
            ScoreQuestion(
                question_id="anger",
                instructions="How angry?",
                levels=("calm", "annoyed", "furious"),
            ),
        ),
    )
    return DecisionRequest(questions=questions, state={"ticket": "charged twice"}, **overrides)


def test_multi_question_normalization_fake_adapter():
    service = DecisionService(
        FakeDecisionAdapter(answers={"route": "billing", "urgent": 0.9, "anger": 1.5})
    )
    response = service.decide(_request())
    assert set(response.results) == {"route", "urgent", "anger"}
    assert response.results["route"].selected_id == "billing"
    assert response.results["route"].probabilities["billing"] == 1.0
    assert response.results["urgent"].probability == 0.9
    assert response.results["anger"].score == 1.5
    # Full distribution retained for calibration, plus adapter identity.
    assert response.provider == "fake-decision"
    assert response.model == FAKE_MODEL_NAME
    assert response.trace_id


def test_fake_adapter_repeatable_without_network(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("fake adapter must not touch the network")

    monkeypatch.setattr(requests, "post", _boom)
    answers = {"route": "technical", "urgent": 0.1, "anger": 0.0}
    first = DecisionService(FakeDecisionAdapter(answers=answers)).decide(_request())
    second = DecisionService(FakeDecisionAdapter(answers=answers)).decide(_request())
    assert first.results["route"].selected_id == second.results["route"].selected_id == "technical"


def test_unknown_choice_rejected():
    adapter = JevAdapter()
    request = _request()
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "route": {"type": "choice", "choice": "refund", "probabilities": {}, "confidence": 0.9},
            "urgent": {"type": "noul", "noul": 0.5},
            "anger": {"type": "score", "score": 1.0, "legend": ANGER_LEGEND, "probabilities": ANGER_PROBABILITIES, "confidence": 0.5},
        },
        "usage": {},
    }
    with pytest.raises(DecisionError) as exc_info:
        adapter.parse_response(data, request)
    assert exc_info.value.kind == "malformed"
    assert "refund" in str(exc_info.value)


def test_unknown_choice_probability_keys_rejected():
    adapter = JevAdapter()
    request = _request()
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "route": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.5, "refund": 0.5},
                "confidence": 0.5,
            },
            "urgent": {"type": "noul", "noul": 0.5},
            "anger": {"type": "score", "score": 1.0, "legend": ANGER_LEGEND, "probabilities": ANGER_PROBABILITIES, "confidence": 0.5},
        },
        "usage": {},
    }
    with pytest.raises(DecisionError) as exc_info:
        adapter.parse_response(data, request)
    assert exc_info.value.kind == "malformed"


def test_jev_payload_shape_matches_systemone_wire():
    request = _request()
    payload = JevAdapter().build_payload(request, model="jev-latest")
    assert payload["model"] == "jev-latest"
    assert payload["state"] == {"ticket": "charged twice"}
    assert payload["questions"]["route"] == {
        "type": "choice",
        "instructions": "Which lane handles this?",
        "criteria": {"billing": "Payment issues", "technical": "Bugs"},
    }
    assert payload["questions"]["urgent"] == {"type": "noul", "instructions": "Is this urgent?"}
    assert payload["questions"]["anger"]["type"] == "score"


def _good_answers():
    return {
        "route": {
            "type": "choice",
            "choice": "billing",
            "probabilities": {"billing": 0.91, "technical": 0.09},
            "confidence": 0.86,
        },
        "urgent": {"type": "noul", "noul": 0.99},
        "anger": {
            "type": "score",
            "score": 1.03,
            "legend": ANGER_LEGEND,
            "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},
            "confidence": 0.84,
        },
    }


@pytest.mark.parametrize(
    "question_id, answer",
    [
        ("urgent", {"type": "noul", "noul": 1.5}),
        ("urgent", {"type": "noul", "noul": float("nan")}),
        ("urgent", {"type": "noul", "noul": True}),
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": "oops", "technical": 0.5},
                "confidence": 0.5,
            },
        ),
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 1.5, "technical": -0.5},
                "confidence": 0.5,
            },
        ),
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.9, "technical": 0.1},
                "confidence": 2.0,
            },
        ),
        (
            "anger",
            {
                "type": "score",
                "score": 999,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},
                "confidence": 0.5,
            },
        ),
        (
            "anger",
            {
                "type": "score",
                "score": 1.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.5, "7": 0.5},
                "confidence": 0.5,
            },
        ),
        (
            "anger",
            {
                "type": "score",
                "score": 1.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": "high", "1": 0.5},
                "confidence": 0.5,
            },
        ),
    ],
)
def test_impossible_numeric_answers_rejected_as_malformed(question_id, answer):
    """Bounded numeric outputs never cross the adapter boundary as valid."""
    request = _request()
    answers = _good_answers()
    answers[question_id] = answer
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(
            {"model": "jev-1.13.0", "answers": answers, "usage": {}}, request
        )
    assert exc_info.value.kind == "malformed"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "question_id, answer",
    [
        # Wrong answer type for the requested kind.
        ("route", {"type": "noul", "noul": 0.5}),
        # Partial choice distribution: missing "technical".
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 1.0},
                "confidence": 0.9,
            },
        ),
        # Missing choice distribution entirely.
        (
            "route",
            {"type": "choice", "choice": "billing", "confidence": 0.9},
        ),
        # Choice distribution that does not sum to one.
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.5, "technical": 0.1},
                "confidence": 0.5,
            },
        ),
        # Missing choice confidence.
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.9, "technical": 0.1},
            },
        ),
        # Partial score distribution: missing level "2".
        (
            "anger",
            {
                "type": "score",
                "score": 1.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.5, "1": 0.5},
                "confidence": 0.5,
            },
        ),
        # Score distribution that does not sum to one.
        (
            "anger",
            {
                "type": "score",
                "score": 1.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.5, "1": 0.5, "2": 0.5},
                "confidence": 0.5,
            },
        ),
        # Missing score confidence.
        (
            "anger",
            {
                "type": "score",
                "score": 1.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},
            },
        ),
    ],
)
def test_incomplete_answer_shapes_rejected_as_malformed(question_id, answer):
    """Partial distributions and missing metadata never become decisions."""
    request = _request()
    answers = _good_answers()
    answers[question_id] = answer
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(
            {"model": "jev-1.13.0", "answers": answers, "usage": {}}, request
        )
    assert exc_info.value.kind == "malformed"
    assert exc_info.value.retryable is False


@pytest.mark.parametrize(
    "legend",
    [
        None,
        {},
        {"0": "calm", "1": "annoyed"},
        {"0": "furious", "1": "annoyed", "2": "calm"},
        {"0": "calm", "1": "annoyed", "2": "furious", "3": "extra"},
    ],
)
def test_missing_or_mismatched_legend_rejected(legend):
    """The numeric score is meaningless without the exact ordered legend."""
    request = _request()
    answers = _good_answers()
    answer = {
        "type": "score",
        "score": 1.0,
        "probabilities": dict(ANGER_PROBABILITIES),
        "confidence": 0.8,
    }
    if legend is not None:
        answer["legend"] = legend
    answers["anger"] = answer
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(
            {"model": "jev-1.13.0", "answers": answers, "usage": {}}, request
        )
    assert exc_info.value.kind == "malformed"


def test_degenerate_distribution_rejected_for_large_candidate_set():
    """The sum tolerance stays bounded: 200 all-zero options are malformed."""
    candidates = tuple(
        DecisionCandidate(id=f"team-{index:03d}", description=f"Team {index}")
        for index in range(200)
    )
    request = DecisionRequest(
        questions=(
            ChoiceQuestion(
                question_id="department",
                instructions="Which team?",
                candidates=candidates,
            ),
        ),
        state="ticket",
    )
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "department": {
                "type": "choice",
                "choice": "team-000",
                "probabilities": {c.id: 0.0 for c in candidates},
                "confidence": 0.0,
            }
        },
        "usage": {},
    }
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(data, request)
    assert exc_info.value.kind == "malformed"


def test_non_unit_distribution_rejected_for_medium_candidate_set():
    """A 20-option map totaling 0.90 is not a distribution, at any size."""
    candidates = tuple(
        DecisionCandidate(id=f"team-{index:02d}", description=f"Team {index}")
        for index in range(20)
    )
    request = DecisionRequest(
        questions=(
            ChoiceQuestion(
                question_id="department",
                instructions="Which team?",
                candidates=candidates,
            ),
        ),
        state="ticket",
    )
    probabilities = {c.id: 0.05 for c in candidates[:10]}
    probabilities.update({c.id: 0.04 for c in candidates[10:]})
    assert abs(sum(probabilities.values()) - 0.90) < 1e-9
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "department": {
                "type": "choice",
                "choice": "team-00",
                "probabilities": probabilities,
                "confidence": 0.2,
            }
        },
        "usage": {},
    }
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(data, request)
    assert exc_info.value.kind == "malformed"


def test_slightly_non_unit_distribution_rejected():
    """A 0.95 total is malformed: the tolerance is small, not 6%."""
    candidates = tuple(
        DecisionCandidate(id=f"team-{index:02d}", description=f"Team {index}")
        for index in range(20)
    )
    request = DecisionRequest(
        questions=(
            ChoiceQuestion(
                question_id="department",
                instructions="Which team?",
                candidates=candidates,
            ),
        ),
        state="ticket",
    )
    probabilities = {c.id: 0.05 for c in candidates[:15]}
    probabilities.update({c.id: 0.04 for c in candidates[15:]})
    assert abs(sum(probabilities.values()) - 0.95) < 1e-9
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "department": {
                "type": "choice",
                "choice": "team-00",
                "probabilities": probabilities,
                "confidence": 0.2,
            }
        },
        "usage": {},
    }
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(data, request)
    assert exc_info.value.kind == "malformed"


def test_invalid_base_url_creates_no_runs(monkeypatch):
    """A bad endpoint is config, not a billable transport attempt."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "not-a-url")
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter(), session_factory=factory).decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state="hello",
            )
        )
    assert exc_info.value.kind == "config"
    with factory() as db:
        assert db.query(AIRun).count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "",
        "http://example.internal/v1/systemone",
        "https://api.typesafe.ai:abc/v1",
        "ftp://localhost/v1/systemone",
    ],
)
def test_insecure_or_malformed_base_url_rejected(monkeypatch, url):
    """Bearer credentials stay off plaintext; bad ports fail preflight."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", url)
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().require_config("jev-latest")
    assert exc_info.value.kind == "config"


def test_loopback_http_endpoint_allowed_for_development(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "http://localhost:8080/v1/systemone")
    JevAdapter().require_config("jev-latest")


def test_base_url_whitespace_normalized_before_use(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv(
        "TYPESAFE_BASE_URL", "  https://api.typesafe.ai/v1/systemone\t"
    )
    adapter = JevAdapter()
    adapter.require_config("jev-latest")
    assert adapter.base_url() == "https://api.typesafe.ai/v1/systemone"


@pytest.mark.parametrize(
    "question_id, answer",
    [
        # "billing" is selected but "technical" dominates the distribution.
        (
            "route",
            {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.10, "technical": 0.90},
                "confidence": 0.8,
            },
        ),
        # Scalar says 0.0 but the distribution means 2.0.
        (
            "anger",
            {
                "type": "score",
                "score": 0.0,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 1.0},
                "confidence": 0.9,
            },
        ),
    ],
)
def test_contradictory_answers_rejected_as_malformed(question_id, answer):
    """Scalar answers must agree with their own distributions."""
    request = _request()
    answers = _good_answers()
    answers[question_id] = answer
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(
            {"model": "jev-1.13.0", "answers": answers, "usage": {}}, request
        )
    assert exc_info.value.kind == "malformed"


@pytest.mark.parametrize(
    "model, usage",
    [
        (None, {}),
        ("", {}),
        (123, {}),
        ("jev-1.13.0", None),
        ("jev-1.13.0", []),
        ("jev-1.13.0", {"input_tokens": -1}),
        ("jev-1.13.0", {"output_tokens": 1.5}),
    ],
)
def test_invalid_response_metadata_rejected(model, usage):
    """Model identity and token accounting are required, typed metadata."""
    request = _request()
    body = {"answers": _good_answers(), "usage": usage}
    if model is not None:
        body["model"] = model
    with pytest.raises(DecisionError) as exc_info:
        JevAdapter().parse_response(body, request)
    assert exc_info.value.kind == "malformed"
    request = _request()
    data = {
        "model": "jev-1.13.0",
        "answers": {
            "route": {
                "type": "choice",
                "choice": "billing",
                "probabilities": {"billing": 0.91, "technical": 0.09},
                "confidence": 0.86,
            },
            "urgent": {"type": "noul", "noul": 0.99},
            "anger": {
                "type": "score",
                "score": 1.03,
                "legend": ANGER_LEGEND,
                "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1},
                "confidence": 0.84,
            },
        },
        "usage": {"input_tokens": 42, "output_tokens": 0},
    }
    results, model, usage = JevAdapter().parse_response(data, request)
    assert results["route"].probabilities == {"billing": 0.91, "technical": 0.09}
    assert results["route"].confidence == 0.86
    assert results["urgent"].probability == 0.99
    assert results["anger"].score == 1.03
    assert model == "jev-1.13.0"
    assert usage == {"input_tokens": 42, "output_tokens": 0}


def test_timeout_bounded_and_retryable(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr("app.decisions.runtime.time.sleep", lambda seconds: None)
    calls = []

    def _boom(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "post", _boom)
    service = DecisionService(JevAdapter())
    with pytest.raises(DecisionError) as exc_info:
        service.decide(_request(timeout_seconds=0.5, max_attempts=2))
    assert exc_info.value.kind == "timeout"
    assert exc_info.value.retryable is True
    assert len(calls) == 2
    assert calls[0] == 0.5


def test_malformed_adapter_response_surfaced(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    class _BadAdapter(JevAdapter):
        def parse_response(self, data, request):
            raise DecisionError("nope", provider=self.name, kind="malformed")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"unexpected": True}

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(_BadAdapter()).decide(_request())
    assert exc_info.value.kind == "malformed"
    assert exc_info.value.retryable is False


def test_invalid_json_response_classified_malformed(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("No JSON object could be decoded")

    monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter()).decide(_request())
    assert exc_info.value.kind == "malformed"
    assert exc_info.value.retryable is False


def test_retry_attempts_keep_failed_succeeded_history(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr("app.decisions.runtime.time.sleep", lambda seconds: None)

    good_body = {
        "model": "jev-1.13.0",
        "answers": {"ok": {"type": "noul", "noul": 0.7}},
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return good_body

    calls = []

    def _flaky(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise requests.Timeout("timed out")
        return _Resp()

    monkeypatch.setattr(requests, "post", _flaky)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = DecisionService(JevAdapter(), session_factory=factory)
    response = service.decide(
        DecisionRequest(
            questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
            state="hello",
            max_attempts=2,
        )
    )
    assert response.results["ok"].probability == 0.7
    with factory() as db:
        runs = db.query(AIRun).order_by(AIRun.attempt).all()
    assert [(run.attempt, run.status) for run in runs] == [(1, "failed"), (2, "succeeded")]
    assert runs[0].error_type == "timeout"
    assert runs[1].parent_run_id == runs[0].id
    assert {run.trace_id for run in runs} == {response.trace_id}
    # First attempt is the billable primary; retries are recovery.
    assert [run.classification for run in runs] == ["primary", "recovery"]
    assert [run.billable for run in runs] == [True, False]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout_seconds": -1},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": "soon"},
        {"max_attempts": 0},
        {"max_attempts": 11},
        {"max_attempts": True},
        {"max_attempts": 2.5},
    ],
)
def test_invalid_execution_knobs_rejected_before_provider_call(kwargs):
    """Bad request controls are malformed requests, never transport errors."""
    adapter = FakeDecisionAdapter(answers={"ok": 0.5})
    service = DecisionService(adapter)
    with pytest.raises(DecisionError) as exc_info:
        service.decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state="hello",
                **kwargs,
            )
        )
    assert exc_info.value.kind == "malformed"
    assert adapter.calls == []


@pytest.mark.parametrize(
    "env, value",
    [
        ("DECISION_TIMEOUT_SECONDS", "oops"),
        ("DECISION_TIMEOUT_SECONDS", "-2"),
        ("DECISION_MAX_ATTEMPTS", "oops"),
        ("DECISION_MAX_ATTEMPTS", "0"),
        ("DECISION_MAX_ATTEMPTS", "99"),
    ],
)
def test_malformed_env_knobs_raise_config(monkeypatch, env, value):
    """Deployment typos surface as config errors, not raw ValueError."""
    monkeypatch.setenv(env, value)
    adapter = FakeDecisionAdapter(answers={"ok": 0.5})
    service = DecisionService(adapter)
    with pytest.raises(DecisionError) as exc_info:
        service.decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state="hello",
            )
        )
    assert exc_info.value.kind == "config"
    assert adapter.calls == []


@pytest.mark.parametrize(
    "env, value",
    [
        ("DECISION_RETRY_BASE_DELAY_SECONDS", "oops"),
        ("DECISION_RETRY_BASE_DELAY_SECONDS", "inf"),
        ("DECISION_RETRY_MAX_DELAY_SECONDS", "-1"),
    ],
)
def test_malformed_retry_delay_env_raises_config(monkeypatch, env, value):
    """A bad backoff knob never masks the original retryable failure."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv(env, value)
    monkeypatch.setattr("app.decisions.runtime.time.sleep", lambda seconds: None)

    def _boom(*args, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter()).decide(_request(max_attempts=2))
    assert exc_info.value.kind == "config"


def test_missing_config_creates_no_runs(monkeypatch):
    """Config failures are not billable provider attempts."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter(), session_factory=factory).decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state="hello",
            )
        )
    assert exc_info.value.kind == "config"
    with factory() as db:
        assert db.query(AIRun).count() == 0


def test_fake_adapter_telemetry_uses_fake_model_identity():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    response = DecisionService(
        FakeDecisionAdapter(answers={"ok": 0.7}), session_factory=factory
    ).decide(
        DecisionRequest(
            questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
            state="hello",
        )
    )
    assert response.model == "fake-decision-model-v1"
    with factory() as db:
        runs = db.query(AIRun).all()
    assert len(runs) == 1
    assert runs[0].model == "fake-decision-model-v1"


def test_request_validation_rejects_bad_shapes():
    service = DecisionService(FakeDecisionAdapter(answers={}))
    with pytest.raises(DecisionError):
        service.decide(DecisionRequest(questions=(), state="s"))
    with pytest.raises(DecisionError):
        service.decide(
            DecisionRequest(
                questions=(
                    NoulQuestion(question_id="a", instructions="x"),
                    NoulQuestion(question_id="a", instructions="y"),
                ),
                state="s",
            )
        )
    with pytest.raises(DecisionError):
        service.decide(
            DecisionRequest(
                questions=(
                    ChoiceQuestion(
                        question_id="c", instructions="pick", candidates=()
                    ),
                ),
                state="s",
            )
        )
    with pytest.raises(DecisionError):
        service.decide(
            DecisionRequest(
                questions=(
                    ScoreQuestion(question_id="s", instructions="rate", levels=("only",)),
                ),
                state="s",
            )
        )


@pytest.mark.parametrize(
    "state",
    [
        {"when": object()},
        {"score": float("nan")},
        {"nested": [{"deep": float("inf")}]},
    ],
)
def test_non_serializable_state_rejected_before_attempt(monkeypatch, state):
    """Local serialization failures are malformed, with no run and no call."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    def _boom(*args, **kwargs):
        raise AssertionError("provider must not be contacted")

    monkeypatch.setattr(requests, "post", _boom)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter(), session_factory=factory).decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state=state,
            )
        )
    assert exc_info.value.kind == "malformed"
    with factory() as db:
        assert db.query(AIRun).count() == 0


def test_missing_state_rejected_before_provider_call(monkeypatch):
    """An omitted state is malformed, never a provider 422 after billing."""
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    def _boom(*args, **kwargs):
        raise AssertionError("provider must not be contacted")

    monkeypatch.setattr(requests, "post", _boom)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter()).decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state=None,
            )
        )
    assert exc_info.value.kind == "malformed"


def test_state_passed_through_verbatim_visibility_safe():
    """The runtime never enriches state: callers authorize before calling."""
    state = {"secret": "redacted-by-caller", "public": "hello"}
    adapter = FakeDecisionAdapter(answers={"ok": 1.0})
    service = DecisionService(adapter)
    service.decide(
        DecisionRequest(
            questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
            state=state,
        )
    )
    assert adapter.calls and adapter.calls[0]["state"] is state


def test_trace_metadata_present():
    service = DecisionService(FakeDecisionAdapter(answers={"ok": 0.2}))
    response = service.decide(
        DecisionRequest(
            questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
            state="hello",
        )
    )
    assert response.trace_id
    assert response.operation_id == response.trace_id
    assert response.latency_ms >= 0
    assert response.provider == "fake-decision"


def test_decision_run_accounted_distinct_from_generative():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    service = DecisionService(
        FakeDecisionAdapter(answers={"ok": 0.7}), session_factory=factory
    )
    response = service.decide(
        DecisionRequest(
            questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
            state="hello",
        )
    )
    with factory() as db:
        runs = db.query(AIRun).all()
    assert len(runs) == 1
    assert runs[0].role == "decision"
    assert runs[0].logical_operation == "bounded_decision"
    assert runs[0].provider == "fake-decision"
    assert runs[0].status == "succeeded"
    assert runs[0].trace_id == response.trace_id


def test_missing_config_without_network(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    service = DecisionService(JevAdapter())
    with pytest.raises(DecisionError) as exc_info:
        service.decide(_request())
    assert exc_info.value.kind == "config"


def test_unsupported_kind_rejected_before_config_or_network():
    """Capability preflight: unsupported kinds never reach the adapter."""

    class _ChoiceOnlyAdapter(FakeDecisionAdapter):
        def capabilities(self):
            return {"choice": True, "noul": False, "score": False}

    adapter = _ChoiceOnlyAdapter(answers={"route": "billing"})
    service = DecisionService(adapter)
    with pytest.raises(DecisionError) as exc_info:
        service.decide(_request())
    assert exc_info.value.kind == "unsupported_feature"
    assert "noul" in str(exc_info.value) and "score" in str(exc_info.value)
    assert adapter.calls == []

    # Supported kinds still execute.
    response = service.decide(
        DecisionRequest(questions=(_request().questions[0],), state="hello")
    )
    assert response.results["route"].selected_id == "billing"


@pytest.mark.parametrize(
    "question",
    [
        ChoiceQuestion(
            question_id="c",
            instructions="pick",
            candidates=("billing", "technical"),
        ),
        ChoiceQuestion(
            question_id="c", instructions="pick", candidates="billing"
        ),
        ScoreQuestion(question_id="s", instructions="rate", levels=1),
        ScoreQuestion(question_id="s", instructions="rate", levels="calm"),
    ],
)
def test_malformed_nested_shapes_rejected(question):
    """Raw containers fail as malformed, never as AttributeError/TypeError."""
    service = DecisionService(FakeDecisionAdapter(answers={}))
    with pytest.raises(DecisionError) as exc_info:
        service.decide(DecisionRequest(questions=(question,), state="hello"))
    assert exc_info.value.kind == "malformed"


@pytest.mark.parametrize(
    "env, value",
    [
        ("TYPESAFE_API_KEY", "   "),
        ("JEV_MODEL", "   "),
    ],
)
def test_whitespace_config_rejected_before_attempt(monkeypatch, env, value):
    """Blank-after-trim credentials are missing config, with no run."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.setenv("JEV_MODEL", "jev-latest")
    monkeypatch.setenv(env, value)

    def _boom(*args, **kwargs):
        raise AssertionError("provider must not be contacted")

    monkeypatch.setattr(requests, "post", _boom)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter(), session_factory=factory).decide(
            DecisionRequest(
                questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
                state="hello",
            )
        )
    assert exc_info.value.kind == "config"
    with factory() as db:
        assert db.query(AIRun).count() == 0


def test_nullable_token_usage_normalizes_successfully():
    """Token counts reported as null are valid provider metadata."""
    request = DecisionRequest(
        questions=(NoulQuestion(question_id="ok", instructions="yes?"),),
        state="hello",
    )
    results, model, usage = JevAdapter().parse_response(
        {
            "model": "jev-latest",
            "usage": {"input_tokens": None, "output_tokens": None},
            "answers": {"ok": {"type": "noul", "noul": 0.7}},
        },
        request,
    )
    assert results["ok"].probability == 0.7
    assert model == "jev-latest"
    assert usage == {"input_tokens": None, "output_tokens": None}


def test_non_serializable_candidate_description_rejected_before_attempt(monkeypatch):
    """Caller-controlled outbound metadata is preflighted before telemetry."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
        SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

    from database import Base
    from models.reliability import AIRun

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")

    def _boom(*args, **kwargs):
        raise AssertionError("provider must not be contacted")

    monkeypatch.setattr(requests, "post", _boom)
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with pytest.raises(DecisionError) as exc_info:
        DecisionService(JevAdapter(), session_factory=factory).decide(
            DecisionRequest(
                questions=(
                    ChoiceQuestion(
                        question_id="route",
                        instructions="Which team?",
                        candidates=(
                            DecisionCandidate(id="billing", description=object()),
                            DecisionCandidate(id="technical", description="Bugs"),
                        ),
                    ),
                ),
                state="hello",
            )
        )
    assert exc_info.value.kind == "malformed"
    with factory() as db:
        assert db.query(AIRun).count() == 0
