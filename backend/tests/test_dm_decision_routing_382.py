"""Decision-first semantic router + bounded fast-path executor — issue #382."""
from __future__ import annotations

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
from models.threads import CampaignThread  # noqa: E402

from app.decisions import (  # noqa: E402
    OPEN_ENDED_DM_CANDIDATE_ID,
    DecisionService,
    get_policy,
)
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.decisions.contracts import ChoiceResult  # noqa: E402
from app.decisions.errors import DecisionError  # noqa: E402
from app.dm import decision_routing as routing  # noqa: E402


def _signals(**overrides):
    kwargs = {
        "state_revision": 3,
        "submission_ids": ("sub-1",),
        "segments": ({"type": "ic", "text": "I look around."},),
    }
    kwargs.update(overrides)
    return routing.RouteSignals(**kwargs)


def _decide(frame, answers):
    service = DecisionService(FakeDecisionAdapter(answers))
    return service.decide(routing.to_decision_request(frame))


def test_route_policy_registered_conservative():
    policy = get_policy(routing.FORWARD_DM_ROUTE_CLASS)
    assert policy.max_risk_for_direct == "low"
    assert policy.near_tie_behavior == "escalate"
    assert policy.min_probability_direct >= 0.8


def test_frame_carries_silent_plus_escapes_and_no_secrets():
    frame = routing.build_route_frame(
        _signals(pending_roll_labels=("Stealth check",))
    )
    ids = {c.id for c in frame.candidates}
    assert routing.ROUTE_SILENT_ID in ids
    assert OPEN_ENDED_DM_CANDIDATE_ID in ids
    blob = json.dumps(frame.state)
    assert "dc_private" not in blob.lower()


def test_supported_silent_intent_completes_without_generative_call():
    frame = routing.build_route_frame(_signals())
    response = _decide(frame, {routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    result = response.results[routing.ROUTE_QUESTION_ID]
    assert isinstance(result, ChoiceResult)
    assert result.selected_id == routing.ROUTE_SILENT_ID

    outcome = routing.decide_from_result(
        frame, result, current_revision=3, submission_ids=("sub-1",)
    )
    assert outcome.directive == "direct_execute"
    assert outcome.contract is not None
    assert outcome.contract.mode == "silent"
    assert outcome.trace["decision_path"] == routing.DECISION_ONLY
    # Code-owned contract: no beats, effects, or model text.
    assert outcome.contract.beats == []
    assert outcome.contract.staged_effects == []


def test_creative_intent_reaches_generative_escape_intact():
    frame = routing.build_route_frame(_signals())
    response = _decide(
        frame, {routing.ROUTE_QUESTION_ID: OPEN_ENDED_DM_CANDIDATE_ID}
    )
    result = response.results[routing.ROUTE_QUESTION_ID]
    outcome = routing.decide_from_result(
        frame, result, current_revision=3, submission_ids=("sub-1",)
    )
    assert outcome.directive == "escalate"
    assert outcome.contract is None
    assert outcome.trace["decision_path"] == routing.OPEN_ENDED_GENERATIVE


def test_middle_policy_band_attaches_advisory_primer():
    frame = routing.build_route_frame(_signals())
    ids = [c.id for c in frame.candidates]
    rest = [i for i in ids if i != routing.ROUTE_SILENT_ID]
    probabilities = {routing.ROUTE_SILENT_ID: 0.60}
    share = 0.40 / len(rest)
    for i in rest:
        probabilities[i] = share
    result = ChoiceResult(
        question_id=routing.ROUTE_QUESTION_ID,
        selected_id=routing.ROUTE_SILENT_ID,
        probabilities=probabilities,
        confidence=0.60,
    )
    outcome = routing.decide_from_result(
        frame, result, current_revision=3, submission_ids=("sub-1",)
    )
    assert outcome.directive == "primer_advisory"
    assert outcome.contract is None
    assert outcome.primer is not None
    assert outcome.primer["selected_id"] == routing.ROUTE_SILENT_ID
    assert outcome.trace["decision_path"] == routing.PRIMED_GENERATIVE


def test_stale_revision_escalates_instead_of_executing():
    frame = routing.build_route_frame(_signals())
    response = _decide(frame, {routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    result = response.results[routing.ROUTE_QUESTION_ID]
    outcome = routing.decide_from_result(
        frame, result, current_revision=4, submission_ids=("sub-1",)
    )
    assert outcome.directive == "escalate"
    assert outcome.contract is None
    assert "revalidation_error" in outcome.trace


def test_unbuildable_route_id_never_synthesizes_contract():
    with pytest.raises(DecisionError):
        routing.build_direct_contract(OPEN_ENDED_DM_CANDIDATE_ID)


def test_decision_adapter_failure_escalates_with_input_intact():
    stub_db = SimpleNamespace(
        scalars=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
        get=lambda model, _id: SimpleNamespace(revision=3),
    )
    attempt = SimpleNamespace(
        id=uuid.uuid4(), campaign_id=uuid.uuid4(), submission_ids=[],
        source_revision=3,
    )
    turn = SimpleNamespace(id=uuid.uuid4())
    service = DecisionService(FakeDecisionAdapter({}))
    outcome = routing.route_attempt(
        stub_db, attempt=attempt, turn=turn, decision_service=service
    )
    assert outcome.directive == "escalate"
    assert outcome.contract is None
    assert outcome.trace["decision_path"] == routing.OPEN_ENDED_GENERATIVE


def test_path_info_fields_distinguish_execution_paths():
    frame = routing.build_route_frame(_signals())
    response = _decide(frame, {routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    result = response.results[routing.ROUTE_QUESTION_ID]
    outcome = routing.decide_from_result(
        frame, result, current_revision=3, submission_ids=("sub-1",)
    )
    fields = routing.path_info_fields(outcome)
    assert fields["decision_path"] == routing.DECISION_ONLY
    assert fields["decision_directive"] == "direct_execute"
    assert fields["decision_selected"] == routing.ROUTE_SILENT_ID


def test_ooc_signal_recorded_for_future_table_chat_route():
    signals = _signals(segments=({"type": "ooc", "text": "brb"},))
    collected = routing.RouteSignals(
        state_revision=signals.state_revision,
        submission_ids=signals.submission_ids,
        segments=signals.segments,
        ooc_only=True,
    )
    frame = routing.build_route_frame(collected)
    assert frame.state["ooc_only"] is True
    # Silent stays enumerable; table_chat direct execution is a follow-up
    # (needs a quality bar for templated OOC intent text).
    assert routing.ROUTE_SILENT_ID in {c.id for c in frame.candidates}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'routing382.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = uuid.uuid4()
    with factory() as s:
        camp_id = uuid.uuid4()
        thread_id = uuid.uuid4()
        s.add(Profile(id=owner, email="owner@example.com"))
        s.add(Campaign(id=camp_id, owner_id=owner, name="Table", revision=0))
        s.add(
            CampaignThread(
                id=thread_id,
                campaign_id=camp_id,
                thread_type="campaign",
                created_by=owner,
            )
        )
        s.commit()
        yield s, camp_id, thread_id


def _submit(s, camp_id, thread_id, text="..."):
    from app.dm.turns import coordinate_turn
    from app.runtime.submissions import accept_submission

    accept_submission(
        s,
        campaign_id=camp_id,
        user_id=s.get(Campaign, camp_id).owner_id,
        raw_content=text,
        segments=[{"type": "ic", "text": text}],
        thread_id=str(thread_id),
    )
    s.commit()
    coord = coordinate_turn(s, camp_id, str(thread_id), commit=False)
    s.commit()
    assert coord is not None
    return coord


def test_direct_silent_bypasses_generative_adjudication(db):
    from app.dm.execution import execute_dm_attempt

    s, camp_id, thread_id = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _must_not_run(packet, feedback=None):
        raise AssertionError("generative adjudication must not run on direct route")

    service = DecisionService(
        FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    )
    result = execute_dm_attempt(
        s, attempt.id, adjudicate=_must_not_run, narrator="deterministic",
        decision_service=service,
    )
    assert result.mode == "silent"
    assert s.get(DmTurn, turn.id).status == "succeeded"
    assert s.get(DmTurnAttempt, attempt.id).status == "succeeded"


def test_generative_path_runs_when_router_escalates(db):
    from app.dm.execution import execute_dm_attempt

    s, camp_id, thread_id = db
    turn, attempt = _submit(s, camp_id, thread_id)
    calls = []

    def _generative(packet, feedback=None):
        calls.append(1)
        from app.dm.contract import CONTRACT_VERSION, normalize_contract
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "silent",
                "reason": "generative escape after OPEN_ENDED_DM",
            }
        )

    service = DecisionService(
        FakeDecisionAdapter(
            {routing.ROUTE_QUESTION_ID: OPEN_ENDED_DM_CANDIDATE_ID}
        )
    )
    result = execute_dm_attempt(
        s, attempt.id, adjudicate=_generative, narrator="deterministic",
        decision_service=service,
    )
    assert calls == [1]
    assert result.mode == "silent"
    assert s.get(DmTurn, turn.id).status == "succeeded"


def _stub_service(result_fn):
    from app.decisions.contracts import DecisionResponse

    class _Stub:
        def __init__(self):
            self.calls = 0

        def decide(self, request):
            self.calls += 1
            return result_fn(request, self.calls)

    return _Stub()


def _choice_response(request, selected_id, probabilities, confidence):
    from app.decisions.contracts import ChoiceResult, DecisionResponse

    qid = request.questions[0].question_id
    return DecisionResponse(
        results={
            qid: ChoiceResult(
                question_id=qid,
                selected_id=selected_id,
                probabilities=probabilities,
                confidence=confidence,
            )
        },
        provider="stub",
        model="stub-model",
        latency_ms=1,
        trace_id="t",
    )


def test_primer_reaches_generative_packet_as_advisory_only(db):
    from app.dm.context import LaneName
    from app.dm.execution import execute_dm_attempt

    s, camp_id, thread_id = db
    turn, attempt = _submit(s, camp_id, thread_id)
    seen = {}

    def _generative(packet, feedback=None):
        seen["packet"] = packet
        from app.dm.contract import CONTRACT_VERSION, normalize_contract
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "silent",
                "reason": "generative escape weighs the advisory prior",
            }
        )

    def _mid_band(request, calls):
        ids = [c.id for c in request.questions[0].candidates]
        rest = [i for i in ids if i != routing.ROUTE_SILENT_ID]
        probs = {routing.ROUTE_SILENT_ID: 0.60}
        for i in rest:
            probs[i] = 0.40 / len(rest)
        return _choice_response(request, routing.ROUTE_SILENT_ID, probs, 0.60)

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=_generative, narrator="deterministic",
        decision_service=_stub_service(_mid_band),
    )
    assert result.mode == "silent"
    assert s.get(DmTurn, turn.id).status == "succeeded"
    packet = seen["packet"]
    lane = next(l for l in packet.lanes if l.name == LaneName.PLAYER_INPUTS)
    primers = [r for r in lane.records if r.record_id.startswith("decision-primer:")]
    assert len(primers) == 1
    assert primers[0].use == "adjudication_only"
    assert primers[0].value["advisory_route"] == routing.ROUTE_SILENT_ID
    assert "not bound" in primers[0].value["authority"]


def test_superseding_input_during_decision_escalates(db):
    s, camp_id, thread_id = db
    turn, attempt = _submit(s, camp_id, thread_id)

    def _supersede_then_answer(request, calls):
        # A newer submission supersedes this attempt mid-decision call.
        fresh_turn = s.get(DmTurn, turn.id)
        fresh_turn.current_attempt_id = uuid.uuid4()
        fresh_turn.input_set_revision = int(fresh_turn.input_set_revision) + 1
        s.add(fresh_turn)
        s.commit()
        ids = [c.id for c in request.questions[0].candidates]
        probs = {i: (1.0 if i == routing.ROUTE_SILENT_ID else 0.0) for i in ids}
        return _choice_response(request, routing.ROUTE_SILENT_ID, probs, 1.0)

    outcome = routing.route_attempt(
        s, attempt=attempt, turn=turn,
        decision_service=_stub_service(_supersede_then_answer),
    )
    assert outcome.directive == "escalate"
    assert outcome.contract is None
    assert "revalidation_error" in outcome.trace


def test_resumed_roll_evidence_escalates_without_decision_call(db):
    from app.dm.execution import execute_dm_attempt

    s, camp_id, thread_id = db
    turn, attempt = _submit(s, camp_id, thread_id)
    fresh_attempt = s.get(DmTurnAttempt, attempt.id)
    fresh_attempt.roll_evidence = [
        {"request_id": "req_1", "status": "fulfilled", "total": 17}
    ]
    s.add(fresh_attempt)
    s.commit()

    def _must_not_decide(request, calls):
        raise AssertionError("no decision call when roll evidence is present")

    outcome = routing.route_attempt(
        s, attempt=fresh_attempt, turn=turn,
        decision_service=_stub_service(_must_not_decide),
    )
    assert outcome.directive == "escalate"
    assert outcome.trace.get("decision_skipped") is True

    calls = []

    def _generative(packet, feedback=None):
        calls.append(1)
        from app.dm.contract import CONTRACT_VERSION, normalize_contract
        return normalize_contract(
            {
                "contract_version": CONTRACT_VERSION,
                "mode": "silent",
                "reason": "generative path resolves the fulfilled roll",
            }
        )

    result = execute_dm_attempt(
        s, attempt.id, adjudicate=_generative, narrator="deterministic",
        decision_service=DecisionService(
            FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
        ),
    )
    assert calls == [1]
    assert result.mode == "silent"


def test_degraded_signal_read_escalates_without_direct_execution():
    from types import SimpleNamespace

    adapter = FakeDecisionAdapter({routing.ROUTE_QUESTION_ID: routing.ROUTE_SILENT_ID})
    service = DecisionService(adapter)
    stub_db = SimpleNamespace(
        scalars=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")),
        get=lambda model, _id: SimpleNamespace(revision=3),
        refresh=lambda *a, **k: None,
    )
    attempt = SimpleNamespace(
        id=uuid.uuid4(), campaign_id=uuid.uuid4(), submission_ids=["sub-1"],
        source_revision=3, roll_evidence=[],
    )
    turn = SimpleNamespace(id=uuid.uuid4())
    outcome = routing.route_attempt(
        stub_db, attempt=attempt, turn=turn, decision_service=service
    )
    assert outcome.directive == "escalate"
    assert outcome.contract is None
    assert outcome.trace.get("decision_skipped") is True
    assert adapter.calls == []
