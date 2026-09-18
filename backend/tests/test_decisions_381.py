"""Decision frames + execution policy tests — issue #381 verification list."""

from __future__ import annotations

import pytest

from app.decisions.adapters.fake import FakeDecisionAdapter
from app.decisions.errors import DecisionError
from app.decisions.frames import (
    CANDIDATE_SCHEMA_VERSION,
    CLARIFY_CANDIDATE_ID,
    DEFER_CANDIDATE_ID,
    FRAME_SCHEMA_VERSION,
    OPEN_ENDED_DM_CANDIDATE_ID,
    CandidateRecord,
    assert_fresh,
    build_frame,
    frame_trace,
    is_stale,
    rebuild_frame,
    resolve_candidate,
    revalidate_for_execution,
    to_decision_request,
)
from app.decisions.policy import (
    POLICY_SCHEMA_VERSION,
    DecisionClassPolicy,
    evaluate_execution,
    get_policy,
    policy_trace,
    register_policy,
)
from app.decisions.runtime import DecisionService


def _frame(**overrides):
    kwargs = {
        "decision_class": "skirmish_action",
        "question_id": "maneuver",
        "instructions": "Which maneuver executes next?",
        "state": {"visible": ["goblin", "archer"], "turn": "fighter-1"},
        "state_revision": 7,
        "candidates": (
            CandidateRecord(
                id="flank",
                label="Flank the goblin",
                source="rules:attack",
                source_ref="state.visible[0]",
                payload_ref="action:flank@goblin",
                debug_hint="advantage via ally",
                risk="low",
                reversible=True,
            ),
            CandidateRecord(
                id="volley",
                label="Loose a volley at the archer",
                source="rules:attack",
                source_ref="state.visible[1]",
                payload_ref="action:volley@archer",
                risk="standard",
                reversible=True,
            ),
        ),
    }
    kwargs.update(overrides)
    return build_frame(**kwargs)


def _probs(selected: str, selected_p: float, others: dict[str, float]) -> dict[str, float]:
    return {selected: selected_p, **others}


def test_legal_selection_resolves_and_round_trips_through_runtime():
    frame = _frame()
    record = resolve_candidate(frame, "flank")
    assert record.payload_ref == "action:flank@goblin"
    assert record.source == "rules:attack"

    request = to_decision_request(frame)
    # Only stable IDs + display labels cross the adapter boundary: payload
    # refs and provenance refs never reach the model.
    sent = {c.id: c.description for c in request.questions[0].candidates}
    assert sent["flank"] == "Flank the goblin"
    payload = FakeDecisionAdapter().build_payload(request, model="fake")
    assert "payload_ref" not in str(payload)
    assert "action:flank@goblin" not in str(payload)

    service = DecisionService(FakeDecisionAdapter(answers={"maneuver": "flank"}))
    response = service.decide(request)
    assert response.results["maneuver"].selected_id == "flank"
    assert resolve_candidate(frame, response.results["maneuver"].selected_id).id == "flank"


def test_unknown_candidate_rejected_as_malformed():
    frame = _frame()
    with pytest.raises(DecisionError) as exc_info:
        resolve_candidate(frame, "fireball-invented-by-model")
    assert exc_info.value.kind == "malformed"
    assert exc_info.value.retryable is False

    # Policy evaluation rejects invented IDs too — never executes them.
    with pytest.raises(DecisionError) as exc_info:
        evaluate_execution(
            frame, "fireball-invented-by-model", {"flank": 0.5}, 0.9, verified=True
        )
    assert exc_info.value.kind == "malformed"


def test_open_ended_dm_escape_defers_instead_of_executing():
    frame = _frame()
    ids = {c.id for c in frame.candidates}
    assert {OPEN_ENDED_DM_CANDIDATE_ID, CLARIFY_CANDIDATE_ID, DEFER_CANDIDATE_ID} <= ids

    verdict = evaluate_execution(
        frame,
        OPEN_ENDED_DM_CANDIDATE_ID,
        _probs(OPEN_ENDED_DM_CANDIDATE_ID, 0.9, {"flank": 0.05, "volley": 0.05}),
        0.95,
        verified=True,
    )
    assert verdict.directive == "escalate"
    assert "open-ended AI DM" in verdict.reason
    assert "human" not in verdict.reason.lower()

    # Revalidation returns the escape record so callers defer, not execute.
    record = revalidate_for_execution(frame, OPEN_ENDED_DM_CANDIDATE_ID, 7)
    assert record.id == OPEN_ENDED_DM_CANDIDATE_ID


def test_stale_revision_rejected_before_execution():
    frame = _frame()
    assert is_stale(frame, 7) is False
    assert is_stale(frame, 8) is True
    with pytest.raises(DecisionError) as exc_info:
        assert_fresh(frame, 8)
    assert exc_info.value.kind == "stale"
    assert exc_info.value.retryable is False

    with pytest.raises(DecisionError) as exc_info:
        revalidate_for_execution(frame, "flank", 8, still_legal=lambda c: True)
    assert exc_info.value.kind == "stale"


def test_rebuild_after_revision_change_refreshes_frame():
    frame = _frame()
    rebuilt = rebuild_frame(
        frame,
        state={"visible": ["goblin"], "turn": "fighter-1"},
        state_revision=8,
    )
    assert rebuilt.state_revision == 8
    assert rebuilt.frame_id != frame.frame_id
    assert is_stale(rebuilt, 8) is False
    assert is_stale(rebuilt, 7) is True
    # Escapes survive the rebuild; stale frame still rejects the new revision.
    assert OPEN_ENDED_DM_CANDIDATE_ID in {c.id for c in rebuilt.candidates}
    with pytest.raises(DecisionError):
        revalidate_for_execution(frame, "flank", 8)


def test_near_tie_policy_defers_per_decision_class():
    frame = _frame()
    verdict = evaluate_execution(
        frame, "flank", {"flank": 0.47, "volley": 0.44}, 0.9, verified=True
    )
    # skirmish_action near-tie margin is 0.10; 0.03 is a near-tie whose
    # configured behavior is primer_advisory, never direct execution.
    assert verdict.directive == "primer_advisory"
    assert "near-tie" in verdict.reason

    strict = DecisionClassPolicy(
        decision_class="custom_strict",
        min_probability_direct=0.6,
        min_confidence_direct=0.6,
        min_margin_direct=0.1,
        near_tie_margin=0.2,
        near_tie_behavior="escalate",
        allow_direct_when_irreversible=False,
        max_risk_for_direct="standard",
        min_probability_primer=0.4,
        min_confidence_primer=0.4,
    )
    register_policy(strict)
    strict_frame = _frame(decision_class="custom_strict")
    verdict = evaluate_execution(
        strict_frame, "flank", {"flank": 0.55, "volley": 0.40}, 0.9,
        verified=True, policy=strict,
    )
    assert verdict.directive == "escalate"

    # Same numbers under the aggressive skirmish class clear direct execution.
    verdict = evaluate_execution(
        frame, "flank", {"flank": 0.55, "volley": 0.40}, 0.9, verified=True
    )
    assert verdict.directive == "direct_execute"


def test_aggressive_reversible_direct_execution():
    frame = _frame()
    verdict = evaluate_execution(
        frame, "flank", {"flank": 0.8, "volley": 0.1}, 0.85, verified=True
    )
    assert verdict.directive == "direct_execute"
    trace = policy_trace(frame, verdict)
    assert trace["candidate_schema_version"] == CANDIDATE_SCHEMA_VERSION
    assert trace["frame_schema_version"] == FRAME_SCHEMA_VERSION
    assert trace["policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert trace["decision_class"] == "skirmish_action"
    assert trace["directive"] == "direct_execute"


def test_higher_risk_and_failed_verification_escalate():
    risky_frame = _frame(
        decision_class="campaign_consequence",
        candidates=(
            CandidateRecord(
                id="burn_bridge",
                label="Burn the bridge with the baron",
                source="rules:social",
                payload_ref="action:burn-bridge@baron",
                risk="high",
                reversible=False,
            ),
            CandidateRecord(
                id="parley",
                label="Parley for one more day",
                source="rules:social",
                payload_ref="action:parley@baron",
                risk="low",
                reversible=True,
            ),
        ),
    )
    policy = get_policy("campaign_consequence")
    # Even a confident high-risk pick escalates: confidence is evidence,
    # never authorization.
    verdict = evaluate_execution(
        risky_frame, "burn_bridge", {"burn_bridge": 0.95}, 0.95,
        verified=True, policy=policy,
    )
    assert verdict.directive == "escalate"
    assert "risk" in verdict.reason

    # Failed deterministic verification escalates even a safe confident pick.
    frame = _frame()
    verdict = evaluate_execution(
        frame, "flank", {"flank": 0.9, "volley": 0.05}, 0.95, verified=False
    )
    assert verdict.directive == "escalate"
    assert "verification" in verdict.reason

    # Irreversible candidates need a primer step even when calibrated.
    irreversible = _frame(
        candidates=(
            CandidateRecord(
                id="shatter",
                label="Shatter the ward-stone",
                source="rules:attack",
                payload_ref="action:shatter@ward",
                risk="low",
                reversible=False,
            ),
            CandidateRecord(
                id="probe",
                label="Probe the ward",
                source="rules:attack",
                payload_ref="action:probe@ward",
                risk="low",
                reversible=True,
            ),
        )
    )
    verdict = evaluate_execution(
        irreversible, "shatter", {"shatter": 0.9}, 0.9, verified=True
    )
    assert verdict.directive == "primer_advisory"


def test_deterministic_revalidation_guards_execution():
    frame = _frame()
    record = revalidate_for_execution(
        frame, "flank", 7, still_legal=lambda c: c.id == "flank"
    )
    assert record.id == "flank"

    # Candidate removed from authoritative state fails revalidation.
    with pytest.raises(DecisionError) as exc_info:
        revalidate_for_execution(frame, "flank", 7, legal_ids={"volley"})
    assert exc_info.value.kind == "malformed"

    with pytest.raises(DecisionError) as exc_info:
        revalidate_for_execution(frame, "flank", 7, still_legal=lambda c: False)
    assert exc_info.value.kind == "malformed"

    # Unknown IDs never revalidate, even at the right revision.
    with pytest.raises(DecisionError) as exc_info:
        revalidate_for_execution(frame, "invented", 7)
    assert exc_info.value.kind == "malformed"


def test_frame_trace_versions_candidate_schema_and_policy():
    frame = _frame()
    trace = frame_trace(frame, policy_version=POLICY_SCHEMA_VERSION)
    assert trace["candidate_schema_version"] == CANDIDATE_SCHEMA_VERSION
    assert trace["frame_schema_version"] == FRAME_SCHEMA_VERSION
    assert trace["policy_schema_version"] == POLICY_SCHEMA_VERSION
    assert set(trace["candidate_ids"]) >= {
        "flank",
        "volley",
        OPEN_ENDED_DM_CANDIDATE_ID,
        CLARIFY_CANDIDATE_ID,
        DEFER_CANDIDATE_ID,
    }
