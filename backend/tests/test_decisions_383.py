"""Decision telemetry, shadow mode, replay, and calibration — issue #383."""
from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.reliability import DecisionTelemetry  # noqa: E402

from app.decisions import (  # noqa: E402
    ACTIVE,
    CandidateRecord,
    DecisionService,
    PolicyVerdict,
    build_frame,
    build_record,
    calibration_summary,
    deserialize_frame,
    evaluate_execution,
    persist_record,
    record_correction,
    record_fail_soft,
    record_ground_truth,
    replay_batch,
    replay_frame,
    runner_up,
    serialize_frame,
    shadow_decide,
    shared_trace,
    to_decision_request,
)
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.decisions.contracts import ChoiceResult  # noqa: E402
from app.decisions.errors import DecisionError  # noqa: E402
from app.decisions.policy import POLICY_SCHEMA_VERSION  # noqa: E402
from app.dm import decision_routing as routing  # noqa: E402


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


def test_correction_signal_stays_distinct_from_ground_truth():
    factory = _setup()
    frame = _frame()
    _, result = _decide(frame, "flank")
    row = record_fail_soft(factory, _record(frame, result, _verdict(frame, result)))
    corrected = record_correction(
        factory, row.id, source="validator_rejection",
        indicates_wrong=True, corrected_to="volley",
    )
    assert corrected.correction_source == "validator_rejection"
    assert corrected.correction_indicates_wrong is True
    assert corrected.ground_truth_id is None
    stamped = record_ground_truth(factory, row.id, "volley")
    assert stamped.ground_truth_id == "volley"
    assert stamped.correction_source == "validator_rejection"
    with factory() as db:
        persisted = db.get(DecisionTelemetry, row.id)
        assert persisted.ground_truth_id == "volley"
        assert persisted.corrected_to == "volley"


def test_correction_validates_inputs():
    factory = _setup()
    frame = _frame()
    _, result = _decide(frame, "flank")
    row = record_fail_soft(factory, _record(frame, result, _verdict(frame, result)))
    with pytest.raises(DecisionError):
        record_correction(factory, row.id, source="  ", indicates_wrong=True)
    with pytest.raises(DecisionError):
        record_correction(factory, row.id, source="x", indicates_wrong="yes")  # type: ignore


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


# --- shadow execution -------------------------------------------------------


def test_shadow_decide_returns_outcome_without_raising():
    frame = _frame()

    def _ok(f):
        _, result = _decide(f, "flank")
        return result, _verdict(f, result)

    outcome = shadow_decide(frame, _ok)
    assert outcome is not None and outcome[0].selected_id == "flank"

    def _boom(f):
        raise DecisionError("adapter down", kind="http")

    assert shadow_decide(frame, _boom) is None


def _stub_db_for_route():
    attempt_id = uuid.uuid4()
    turn_id = uuid.uuid4()
    thread_id = uuid.uuid4()
    camp_id = uuid.uuid4()
    fresh_attempt = SimpleNamespace(
        id=attempt_id, turn_id=turn_id, status="prepared", submission_ids=[],
        input_set_revision=0, roll_evidence=[], source_revision=3,
        campaign_id=camp_id, thread_id=str(thread_id), audience="campaign",
    )
    fresh_turn = SimpleNamespace(
        id=turn_id, current_attempt_id=attempt_id, input_set_revision=0,
        submission_ids=[], campaign_id=camp_id, thread_id=str(thread_id),
        audience="campaign",
    )
    # Issue #248 — the router authorizes the attempt against its thread
    # before building any frame, so stubs must supply thread authority.
    stub_thread = SimpleNamespace(
        id=thread_id, campaign_id=camp_id, thread_type="campaign",
    )

    def _scalars(*args, **kwargs):
        return SimpleNamespace(all=lambda: [])

    def _get(model, _id):
        name = getattr(model, "__name__", "")
        if name == "Campaign":
            return SimpleNamespace(revision=3)
        if name == "DmTurnAttempt":
            return fresh_attempt
        if name == "DmTurn":
            return fresh_turn
        if name == "CampaignThread":
            return stub_thread if str(_id) == str(thread_id) else None
        return None

    return SimpleNamespace(
        scalars=_scalars, get=_get, refresh=lambda *a, **k: None,
    ), fresh_attempt, fresh_turn


def test_shadow_route_records_comparable_decision_without_changing_path():
    stub_db, attempt, turn = _stub_db_for_route()
    attempt = SimpleNamespace(
        id=attempt.id, campaign_id=attempt.campaign_id, turn_id=turn.id,
        thread_id=attempt.thread_id, audience="campaign",
        submission_ids=[], source_revision=3, status="prepared",
        input_set_revision=0, roll_evidence=[],
    )
    service = DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    )
    active = routing.route_attempt(
        stub_db, attempt=attempt, turn=turn, decision_service=service,
    )
    assert active.directive == "direct_execute"
    assert active.contract is not None

    shadow = routing.route_attempt(
        stub_db, attempt=attempt, turn=turn, decision_service=service, shadow=True,
    )
    # Authoritative path continues unchanged: always the generative escape.
    assert shadow.directive == "escalate"
    assert shadow.contract is None
    assert shadow.trace["decision_path"] == routing.OPEN_ENDED_GENERATIVE
    assert shadow.trace["shadow"] is True
    # ...while recording the comparable live decision for calibration.
    assert shadow.trace["shadow_selected"] == active.selected_id == routing.ROUTE_SILENT_ID
    assert shadow.trace["shadow_directive"] == "direct_execute"


# --- replay -----------------------------------------------------------------


def _replay_decide_fn(selected_id, model="replay-model-v2"):
    def _fn(frame):
        service = DecisionService(FakeDecisionAdapter({frame.question_id: selected_id}))
        response = service.decide(to_decision_request(frame))
        result = response.results[frame.question_id]
        verdict = evaluate_execution(
            frame, result.selected_id, dict(result.probabilities),
            result.confidence, verified=True,
        )
        return result, verdict, model

    return _fn


def test_replay_read_only_against_new_model_version():
    factory = _setup()
    frame = _frame()
    stored = serialize_frame(frame)
    frozen = copy.deepcopy(stored)

    outcome = replay_frame(stored, _replay_decide_fn("volley"))
    assert outcome.selected_id == "volley"
    assert outcome.directive in ("direct_execute", "primer_advisory", "escalate")
    assert outcome.replay_model == "replay-model-v2"
    assert outcome.replay_policy_version == POLICY_SCHEMA_VERSION
    # Stored snapshot untouched; replay wrote no telemetry rows.
    assert stored == frozen
    with factory() as db:
        assert db.query(DecisionTelemetry).count() == 0


def test_replay_batch_skips_corrupt_frames_without_side_effects():
    frame = _frame()
    stored = [serialize_frame(frame), {"telemetry_schema_version": 1, "bogus": True}]
    outcomes = replay_batch(stored, _replay_decide_fn("flank"))
    assert len(outcomes) == 1
    assert outcomes[0].selected_id == "flank"


def test_replay_deserialize_rejects_malformed_snapshot():
    with pytest.raises(DecisionError):
        deserialize_frame({"decision_class": "x"})


# --- calibration ------------------------------------------------------------


def _calibration_records():
    frame = _frame()
    out = []

    def _mk(selected, confidence, directive, truth=None, wrong=None, source=None):
        _, result = _decide(frame, selected)
        base = _verdict(frame, result)
        verdict = PolicyVerdict(
            directive=directive, reason="test", decision_class=base.decision_class,
            selected_id=base.selected_id, probability=confidence,
            confidence=confidence, margin=base.margin,
        )
        record = _record(frame, result, verdict)
        return record.__class__(**{
            **record.__dict__, "confidence": confidence,
            "ground_truth_id": truth,
            "correction_source": source,
            "correction_indicates_wrong": wrong,
        })

    # Role skirmish_action, top bucket: 2 direct-correct, 1 direct-wrong,
    # 1 escalate that would have been right (unnecessary escalation).
    out.append(_mk("flank", 0.9, "direct_execute", truth="flank"))
    out.append(_mk("flank", 0.92, "direct_execute", truth="flank"))
    out.append(_mk("volley", 0.95, "direct_execute", truth="flank"))
    out.append(_mk("flank", 0.91, "escalate", truth="flank"))
    # Correction-evidence wrongness (no ground truth) in the same role.
    out.append(_mk("volley", 0.5, "direct_execute", wrong=True, source="repair_loop"))
    # A second role stays separate — never merged into a global metric.
    other = _frame(decision_class="campaign_consequence")
    _, result = _decide(other, "flank")
    verdict = _verdict(other, result)
    rec = _record(other, result, verdict)
    out.append(rec.__class__(**{
        **rec.__dict__, "confidence": 0.9, "ground_truth_id": "flank",
    }))
    return out


def test_calibration_summary_is_per_role_with_error_rates():
    summary = calibration_summary(_calibration_records(), buckets=10)
    assert set(summary) == {"skirmish_action", "campaign_consequence"}
    assert "global" not in summary
    top = summary["skirmish_action"][9]
    assert top.n == 4
    assert top.accuracy == pytest.approx(0.75)
    assert top.direct_execute_error_rate == pytest.approx(1 / 3)
    assert top.unnecessary_escalation_rate == pytest.approx(1.0)
    mid = summary["skirmish_action"][5]
    assert mid.n == 1
    assert mid.accuracy == pytest.approx(0.0)
    assert mid.correction_rate == pytest.approx(1.0)
    other_top = summary["campaign_consequence"][9]
    assert other_top.n == 1 and other_top.accuracy == pytest.approx(1.0)
    empty = summary["skirmish_action"][0]
    assert empty.n == 0 and empty.accuracy is None


def test_calibration_rejects_bad_bucket_count():
    with pytest.raises(DecisionError):
        calibration_summary([], buckets=0)


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


def _route_frame(**overrides):
    kwargs = {
        "state_revision": 3,
        "submission_ids": ("sub-1",),
        "segments": ({"type": "ic", "text": "I look around."},),
    }
    kwargs.update(overrides)
    return routing.build_route_frame(routing.RouteSignals(**kwargs))


def test_routing_telemetry_keeps_policy_directive_distinct_from_revalidation():
    factory = _setup()
    frame = _route_frame()
    service = DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    )
    response = service.decide(routing.to_decision_request(frame))
    result = response.results[routing.ROUTE_QUESTION_ID]
    # Policy clears direct_execute, but the authoritative revision moved on,
    # so deterministic revalidation fails and routing escalates.
    outcome = routing.decide_from_result(
        frame, result, current_revision=999,
        frame_submission_ids=("sub-1",), current_submission_ids=("sub-1",),
    )
    assert outcome.directive == "escalate"
    assert "revalidation_error" in outcome.trace
    assert outcome.trace["directive"] == "direct_execute"

    campaign_id, turn_id = uuid.uuid4(), uuid.uuid4()
    routing._record_routing_telemetry(
        object(), frame=frame, result=result, response=response,
        outcome=outcome, mode=ACTIVE, trace_id="trace-reval",
        campaign_id=campaign_id, turn_id=turn_id, session_factory=factory,
    )
    with factory() as db:
        row = db.query(DecisionTelemetry).one()
        assert row.policy_directive == "direct_execute"
        assert row.verified is False
        assert row.revalidation_error is not None
        assert row.mode == ACTIVE
        assert str(row.campaign_id) == str(campaign_id)
        assert str(row.turn_id) == str(turn_id)
        assert row.trace_id == "trace-reval"
        assert row.operation_id == response.operation_id


def test_routing_telemetry_persists_live_correlation_ids():
    factory = _setup()
    frame = _route_frame()
    service = DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    )
    response = service.decide(routing.to_decision_request(frame))
    result = response.results[routing.ROUTE_QUESTION_ID]
    outcome = routing.decide_from_result(
        frame, result, current_revision=3, submission_ids=("sub-1",),
    )
    assert outcome.directive == "direct_execute"

    campaign_id, turn_id = uuid.uuid4(), uuid.uuid4()
    routing._record_routing_telemetry(
        object(), frame=frame, result=result, response=response,
        outcome=outcome, mode=ACTIVE, trace_id=None,
        campaign_id=campaign_id, turn_id=turn_id, session_factory=factory,
    )
    with factory() as db:
        row = db.query(DecisionTelemetry).one()
        assert row.policy_directive == "direct_execute"
        assert row.verified is True
        # Runtime correlation falls back to the adapter response when the
        # caller supplies no trace override.
        assert row.trace_id == response.trace_id
        assert row.operation_id == response.operation_id
        assert str(row.campaign_id) == str(campaign_id)
        assert str(row.turn_id) == str(turn_id)


def test_superseded_attempt_still_records_policy_outcome_and_revalidation():
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import sessionmaker as _sessionmaker

    engine = _create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = _sessionmaker(bind=engine, expire_on_commit=False)
    owner_id, camp_id, turn_id, attempt_id, thread_id = (uuid.uuid4() for _ in range(5))
    with factory() as db:
        db.add(Profile(id=owner_id, email="owner@example.com"))
        db.add(Campaign(id=camp_id, owner_id=owner_id, name="Table", revision=3))
        # Issue #248 — the router authorizes the attempt against its thread
        # before building any frame, so the attempt needs a real thread row
        # whose type matches its audience.
        from models.threads import CampaignThread

        db.add(CampaignThread(
            id=thread_id, campaign_id=camp_id, thread_type="campaign",
            created_by=owner_id,
        ))
        db.add(DmTurn(
            id=turn_id, campaign_id=camp_id, thread_id=str(thread_id),
            source_revision=3, input_set_revision=0, submission_ids=[],
            current_attempt_id=attempt_id,
        ))
        db.add(DmTurnAttempt(
            id=attempt_id, turn_id=turn_id, attempt_number=1,
            campaign_id=camp_id, thread_id=str(thread_id),
            source_revision=3, input_set_revision=0, submission_ids=[],
            # A newer submission superseded this attempt mid-decision.
            status="superseded",
        ))
        db.commit()
    service = DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    )
    with factory() as db:
        attempt = db.get(DmTurnAttempt, attempt_id)
        turn = db.get(DmTurn, turn_id)
        outcome = routing.route_attempt(
            db, attempt=attempt, turn=turn, decision_service=service,
            trace_id="trace-superseded",
        )
        # Authoritative path escalates unchanged...
        assert outcome.directive == "escalate"
        assert outcome.contract is None
        assert "revalidation_error" in outcome.trace
    # ...while the evaluated decision is recorded with its policy outcome,
    # failed revalidation, and correlation IDs.
    with factory() as db:
        row = db.query(DecisionTelemetry).one()
        assert row.policy_directive == "direct_execute"
        assert row.verified is False
        assert "superseded" in (row.revalidation_error or "")
        assert row.mode == ACTIVE
        assert str(row.campaign_id) == str(camp_id)
        assert str(row.turn_id) == str(turn_id)
        assert row.trace_id == "trace-superseded"
        assert row.selected_id == routing.ROUTE_SILENT_ID


def test_non_wrong_correction_is_unknown_until_ground_truth_exists():
    frame = _frame()

    def _mk(selected, confidence, directive, truth=None, wrong=None, source=None):
        _, result = _decide(frame, selected)
        base = _verdict(frame, result)
        verdict = PolicyVerdict(
            directive=directive, reason="test", decision_class=base.decision_class,
            selected_id=base.selected_id, probability=confidence,
            confidence=confidence, margin=base.margin,
        )
        record = _record(frame, result, verdict)
        return record.__class__(**{
            **record.__dict__, "confidence": confidence,
            "ground_truth_id": truth,
            "correction_source": source,
            "correction_indicates_wrong": wrong,
        })

    records = [
        _mk("flank", 0.90, "direct_execute", truth="flank"),
        # A correction signal that explicitly does NOT indicate wrongness
        # is not proof the decision was right...
        _mk("volley", 0.92, "direct_execute",
            wrong=False, source="validator_review"),
        _mk("flank", 0.91, "escalate", wrong=False, source="validator_review"),
    ]
    top = calibration_summary(records, buckets=10)["skirmish_action"][9]
    assert top.n == 3
    # ...so only the ground-truth record enters the known-outcome
    # denominator: accuracy 1/1, not 3/3.
    assert top.accuracy == pytest.approx(1.0)
    assert top.direct_execute_error_rate == pytest.approx(0.0)
    # No known escalated outcome: nothing to call unnecessarily escalated.
    assert top.unnecessary_escalation_rate is None
    assert top.correction_rate == pytest.approx(2 / 3)
