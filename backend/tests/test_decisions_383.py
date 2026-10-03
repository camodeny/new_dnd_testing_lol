"""Decision telemetry — issue #383."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from models.reliability import DecisionTelemetry  # noqa: E402

from app.decisions import (  # noqa: E402
    ACTIVE,
    CandidateRecord,
    DecisionService,
    PolicyVerdict,
    build_frame,
    build_record,
    evaluate_execution,
    persist_record,
    record_fail_soft,
    runner_up,
    shared_trace,
    to_decision_request,
)
from tests.support.fake_decisions import FakeDecisionAdapter  # noqa: E402
from app.decisions.contracts import ChoiceResult  # noqa: E402
from app.decisions.errors import DecisionError  # noqa: E402
from app.decisions.policy import POLICY_SCHEMA_VERSION  # noqa: E402
from tests.support.decision_policies import register_example_policies  # noqa: E402

register_example_policies()


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory


def _frame(**overrides):
    kwargs = {
        "decision_class": "skirmish_action",
        "question_id": "maneuver",
        "instructions": "Which maneuver executes next?",
        "state": {"visible": ["goblin"], "turn": "fighter-1"},
        "state_revision": 7,
        "candidates": (
            CandidateRecord(
                id="flank", label="Flank the SECRET goblin redoubt",
                source="rules:attack", risk="low", reversible=True,
            ),
            CandidateRecord(
                id="volley", label="Loose a volley",
                source="rules:attack", risk="standard", reversible=True,
            ),
        ),
    }
    kwargs.update(overrides)
    return build_frame(**kwargs)


def _decide(frame, selected_id, confidence=0.9):
    service = DecisionService(FakeDecisionAdapter({frame.question_id: selected_id}))
    response = service.decide(to_decision_request(frame))
    result = response.results[frame.question_id]
    assert isinstance(result, ChoiceResult)
    return response, result


def _verdict(frame, result):
    return evaluate_execution(
        frame, result.selected_id, dict(result.probabilities),
        result.confidence, verified=True,
    )


def _record(frame, result, verdict, **overrides):
    kwargs = {
        "provider": "fake-decision",
        "model": "fake-decision-model-v1",
        "mode": ACTIVE,
        "trace_id": "trace-383",
        "latency_ms": 12,
    }
    kwargs.update(overrides)
    return build_record(frame, result, verdict, **kwargs)


# --- record construction ----------------------------------------------------


def test_record_captures_full_reconstruction_context():
    frame = _frame()
    response, result = _decide(frame, "flank")
    verdict = _verdict(frame, result)
    record = _record(
        frame, result, verdict, campaign_id=uuid.uuid4(), turn_id=uuid.uuid4(),
        operation_id="op-1", verified=True,
    )
    assert record.decision_class == "skirmish_action"
    assert set(record.candidate_ids) >= {"flank", "volley"}
    assert record.selected_id == "flank"
    assert record.probabilities["flank"] == 1.0
    assert record.runner_up_id != "flank" and record.margin == 1.0
    assert record.confidence == 1.0
    assert record.policy_directive == verdict.directive
    assert record.verified is True
    assert record.frame_id == frame.frame_id
    assert record.state_revision == "7"
    assert record.candidate_schema_version == 1
    assert record.frame_schema_version == 1
    assert record.policy_schema_version == POLICY_SCHEMA_VERSION
    assert record.latency_ms == response.latency_ms or record.latency_ms == 12


def test_record_rejects_unknown_mode():
    frame = _frame()
    _, result = _decide(frame, "flank")
    verdict = _verdict(frame, result)
    with pytest.raises(DecisionError):
        _record(frame, result, verdict, mode="sneaky")


def test_record_rejects_mismatched_question():
    frame = _frame()
    _, result = _decide(frame, "flank")
    verdict = _verdict(frame, result)
    other = _frame(question_id="other-q")
    with pytest.raises(DecisionError):
        build_record(other, result, verdict, provider="p", model="m")


def test_runner_up_tie_breaks_deterministically():
    second, margin = runner_up({"a": 0.4, "b": 0.3, "c": 0.3}, "a")
    assert second == "b" and margin == pytest.approx(0.1)


# --- persistence ------------------------------------------------------------


def test_probability_distribution_survives_persistence():
    factory = _setup()
    frame = _frame()
    _, result = _decide(frame, "volley")
    verdict = _verdict(frame, result)
    record = _record(frame, result, verdict, mode=ACTIVE)
    row = record_fail_soft(factory, record)
    assert row is not None
    with factory() as db:
        persisted = db.get(DecisionTelemetry, row.id)
        assert persisted.probabilities == record.probabilities
        assert persisted.selected_id == "volley"
        assert persisted.runner_up_id == record.runner_up_id
        assert persisted.margin == pytest.approx(record.margin)
        assert persisted.policy_directive == verdict.directive
        assert persisted.mode == ACTIVE
        assert persisted.verified is None


def test_policy_outcome_and_revalidation_logged_per_directive():
    factory = _setup()
    frame = _frame()
    for selected, directive, verified, err in (
        ("flank", "direct_execute", True, None),
        ("volley", "primer_advisory", None, None),
        ("volley", "escalate", False, "stale revision"),
    ):
        _, result = _decide(frame, selected)
        base = _verdict(frame, result)
        verdict = PolicyVerdict(
            directive=directive, reason="test", decision_class=base.decision_class,
            selected_id=base.selected_id, probability=base.probability,
            confidence=base.confidence, margin=base.margin,
        )
        record = _record(
            frame, result, verdict, verified=verified, revalidation_error=err,
        )
        row = record_fail_soft(factory, record)
        with factory() as db:
            persisted = db.get(DecisionTelemetry, row.id)
            assert persisted.policy_directive == directive
            assert persisted.verified is verified
            assert persisted.revalidation_error == err


# --- privacy ----------------------------------------------------------------


def test_shared_trace_and_row_carry_no_private_state():
    frame = _frame()
    _, result = _decide(frame, "flank")
    record = _record(frame, result, _verdict(frame, result))
    blob = json.dumps(shared_trace(record))
    assert "SECRET" not in blob
    assert "goblin" not in blob
    assert "fighter-1" not in blob
    assert "flank" in blob  # stable IDs are permitted
    assert set(shared_trace(record)) == {
        "decision_class", "question_id", "question_kind", "provider", "model",
        "model_version", "candidate_schema_version", "frame_schema_version",
        "policy_schema_version", "telemetry_schema_version", "candidate_ids",
        "selected_id", "probabilities", "runner_up_id", "margin", "confidence",
        "latency_ms", "cost_usd", "trace_id", "operation_id", "campaign_id",
        "turn_id", "frame_id", "state_revision", "mode", "policy_directive",
        "verified", "revalidation_error",
    }
    factory = _setup()
    row = record_fail_soft(factory, record)
    with factory() as db:
        persisted = db.get(DecisionTelemetry, row.id)
        row_blob = json.dumps(
            {"ids": persisted.candidate_ids, "selected": persisted.selected_id,
             "probs": persisted.probabilities, "mode": persisted.mode}
        )
        assert "SECRET" not in row_blob


# --- observability failure isolation ----------------------------------------


def test_telemetry_failure_never_breaks_gameplay():
    factory = _setup()

    def _broken():
        raise RuntimeError("telemetry database unavailable")

    frame = _frame()
    _, result = _decide(frame, "flank")
    record = _record(frame, result, _verdict(frame, result))
    assert record_fail_soft(None, record) is None
    assert record_fail_soft(_broken, record) is None
    # Gameplay path still works afterwards on the healthy factory.
    assert record_fail_soft(factory, record) is not None
    with factory() as db:
        assert db.query(DecisionTelemetry).count() == 1


def test_telemetry_writes_reject_a_gameplay_session():
    factory = _setup()
    with factory() as gameplay_db:
        frame = _frame()
        _, result = _decide(frame, "flank")
        record = _record(frame, result, _verdict(frame, result))
        with pytest.raises(TypeError, match="dedicated session factory"):
            persist_record(gameplay_db, record)
        # ...while the fail-soft gameplay path degrades to None, never raising.
        assert record_fail_soft(gameplay_db, record) is None
