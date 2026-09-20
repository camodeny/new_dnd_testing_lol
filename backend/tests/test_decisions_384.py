"""Semantic judge layer for ambiguous validation — issue #384."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
from models.reliability import DecisionTelemetry  # noqa: E402

from app.decisions import (  # noqa: E402
    ACTIVE,
    ALL_JUDGE_QUESTIONS,
    JUDGE_CANON_CONFLICT,
    JUDGE_CONTRADICTION,
    JUDGE_ESCALATE,
    JUDGE_PASS,
    JUDGE_PC_AGENCY,
    JUDGE_POLICY_VERSION,
    JUDGE_REGENERATE,
    JUDGE_REPAIR,
    JUDGE_SCHEMA_VERSION,
    JUDGE_SECRECY_RISK,
    JUDGE_UNSUPPORTED_ADDITION,
    BufferedJudgeCheckpoint,
    CanonEntry,
    DecisionService,
    JudgePolicy,
    StreamJudgePolicy,
    build_evidence,
    build_judge_questions,
    build_judge_records,
    build_judge_request,
    calibration_summary,
    evaluate_judges,
    format_judge_feedback,
    judge_retry_allowed,
    judge_trace,
    record_judge_rows,
    run_judges,
    shadow_judge,
    validator_residue_summary,
)
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.decisions.contracts import ChoiceResult, NoulResult  # noqa: E402
from app.decisions.errors import DecisionError  # noqa: E402
from app.decisions.telemetry import SHADOW  # noqa: E402


def _setup():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _evidence(**overrides):
    kwargs = {
        "candidate_text": "Lyra studies the cracked vault door. She says nothing.",
        "public_claim_texts": ("Lyra studies the cracked vault door.",),
        "declaration_texts": ("I study the vault door.",),
        "canon_entries": (
            CanonEntry(canonical="the vault door is cracked", forbids=("intact",)),
        ),
        "secret_texts": ("The vault hides the émigré ledger.",),
        "pc_tokens": ("lyra",),
        "evidence_revision": "rev-1",
    }
    kwargs.update(overrides)
    return build_evidence(**kwargs)


def _service(answers):
    return DecisionService(FakeDecisionAdapter(dict(answers)))


def _clean_answers(probability=0.05):
    return {qid: probability for qid in ALL_JUDGE_QUESTIONS}


# --- question construction --------------------------------------------------


def test_builds_all_five_judge_questions_by_default():
    questions = build_judge_questions(_evidence())
    assert [q.question_id for q in questions] == list(ALL_JUDGE_QUESTIONS)
    assert all(q.kind == "noul" for q in questions)
    assert all(q.instructions.strip() for q in questions)


def test_rejects_unknown_duplicate_or_empty_question_sets():
    with pytest.raises(DecisionError):
        build_judge_questions(_evidence(), only=("judge_made_up",))
    with pytest.raises(DecisionError):
        build_judge_questions(
            _evidence(), only=(JUDGE_PC_AGENCY, JUDGE_PC_AGENCY)
        )
    with pytest.raises(DecisionError):
        build_judge_questions(_evidence(), only=())


def test_request_fans_out_one_call_over_shared_candidate_state():
    request = build_judge_request(_evidence())
    assert len(request.questions) == 5
    assert request.state["candidate_text"].startswith("Lyra studies")
    assert request.state["judge_schema_version"] == JUDGE_SCHEMA_VERSION


def test_evidence_bounds_lists_and_text():
    evidence = build_evidence(
        "x" * 9000,
        public_claim_texts=[f"claim {i}" for i in range(40)],
        secret_texts=[f"secret {i}" for i in range(40)],
    )
    assert len(evidence.candidate_text) <= 4001
    assert len(evidence.public_claim_texts) == 12
    assert len(evidence.secret_texts) == 8


# --- pass / harmless observation --------------------------------------------


def test_harmless_observation_passes():
    verdict, response = run_judges(
        _service(_clean_answers()),
        _evidence(),
        deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_PASS
    assert verdict.failed_questions == ()
    assert verdict.deterministic_final is False
    assert len(response.results) == 5


def test_single_decision_call_answers_every_question():
    service = _service(_clean_answers())
    run_judges(service, _evidence(), deterministic_passed=True)
    assert len(service.adapter.calls) == 1
    assert service.adapter.calls[0]["questions"] == sorted(ALL_JUDGE_QUESTIONS)


# --- agency invention without verb lists ------------------------------------


def test_subtle_invented_pc_behavior_escalates():
    evidence = _evidence(
        candidate_text=(
            "Lyra weighs the cracked door for a moment, quietly resolving "
            "that the vault's contents should be hers alone."
        ),
    )
    answers = _clean_answers()
    answers[JUDGE_PC_AGENCY] = 0.9
    verdict, _ = run_judges(
        _service(answers), evidence, deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_ESCALATE
    assert JUDGE_PC_AGENCY in verdict.failed_questions
    assert verdict.findings[JUDGE_PC_AGENCY].probability == pytest.approx(0.9)


def test_supported_consequence_is_not_an_unsupported_addition():
    answers = _clean_answers()
    answers[JUDGE_UNSUPPORTED_ADDITION] = 0.12
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_PASS


def test_unsupported_addition_repairs_with_targeted_feedback():
    answers = _clean_answers()
    answers[JUDGE_UNSUPPORTED_ADDITION] = 0.82
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_REPAIR
    feedback = format_judge_feedback(verdict)
    assert JUDGE_UNSUPPORTED_ADDITION in feedback
    assert "émigré ledger" not in feedback


def test_contradiction_and_canon_conflict_repair():
    for qid in (JUDGE_CONTRADICTION, JUDGE_CANON_CONFLICT):
        answers = _clean_answers()
        answers[qid] = 0.75
        verdict, _ = run_judges(
            _service(answers), _evidence(), deterministic_passed=True,
        )
        assert verdict.directive == JUDGE_REPAIR, qid


def test_secret_leakage_escalates_despite_literal_pass():
    answers = _clean_answers()
    answers[JUDGE_SECRECY_RISK] = 0.85
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_ESCALATE
    assert JUDGE_SECRECY_RISK in verdict.failed_questions


# --- deterministic checks stay final ----------------------------------------


def test_semantic_pass_cannot_overturn_deterministic_failure():
    verdict, _ = run_judges(
        _service(_clean_answers(probability=0.0)),
        _evidence(),
        deterministic_passed=False,
        deterministic_codes=["pc_action_by_non_owner"],
    )
    assert verdict.directive == JUDGE_ESCALATE
    assert verdict.deterministic_final is True
    assert "pc_action_by_non_owner" in verdict.reason


def test_deterministic_ownership_failure_final_when_judges_also_fail():
    answers = _clean_answers()
    answers[JUDGE_PC_AGENCY] = 0.95
    verdict, _ = run_judges(
        _service(answers),
        _evidence(),
        deterministic_passed=False,
        deterministic_codes=["unknown_source_ref"],
    )
    assert verdict.directive == JUDGE_ESCALATE
    assert verdict.deterministic_final is True


def test_deterministic_flag_must_be_boolean():
    with pytest.raises(DecisionError):
        evaluate_judges({}, deterministic_passed="yes")  # type: ignore[arg-type]


# --- bounded retry / escalation ---------------------------------------------


def test_retry_exhaustion_escalates_instead_of_looping():
    answers = _clean_answers()
    answers[JUDGE_CONTRADICTION] = 0.8
    policy = JudgePolicy(max_regenerations=1)
    early, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
        attempts_used=0, policy=policy,
    )
    assert early.directive == JUDGE_REPAIR
    assert judge_retry_allowed(early, attempts_used=0) is True
    late, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
        attempts_used=1, policy=policy,
    )
    assert late.directive == JUDGE_ESCALATE
    assert judge_retry_allowed(late, attempts_used=1) is False


def test_pass_and_escalate_never_retry():
    verdict, _ = run_judges(
        _service(_clean_answers()), _evidence(), deterministic_passed=True,
    )
    assert judge_retry_allowed(verdict, attempts_used=0) is False


def test_policy_thresholds_are_per_question():
    policy = JudgePolicy(
        fail_thresholds={JUDGE_CANON_CONFLICT: 0.9},
    )
    answers = _clean_answers()
    answers[JUDGE_CANON_CONFLICT] = 0.75
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
        policy=policy,
    )
    assert verdict.directive == JUDGE_PASS
    assert policy.threshold_for(JUDGE_CANON_CONFLICT) == 0.9
    assert policy.threshold_for(JUDGE_PC_AGENCY) == 0.5


def test_policy_rejects_bad_thresholds_and_budgets():
    with pytest.raises(DecisionError):
        JudgePolicy(fail_thresholds={JUDGE_PC_AGENCY: 1.5})
    with pytest.raises(DecisionError):
        JudgePolicy(max_regenerations=99)


# --- malformed judge answers -------------------------------------------------


def test_missing_or_extra_answers_raise():
    evidence = _evidence()
    service = _service({JUDGE_PC_AGENCY: 0.1})
    with pytest.raises(DecisionError):
        run_judges(service, evidence, deterministic_passed=True)
    with pytest.raises(DecisionError):
        evaluate_judges(
            {qid: NoulResult(question_id=qid, probability=0.1)
             for qid in ALL_JUDGE_QUESTIONS} | {"extra": NoulResult(question_id="extra", probability=0.1)},
            deterministic_passed=True,
        )


def test_non_noul_or_out_of_range_answers_raise():
    good = {
        qid: NoulResult(question_id=qid, probability=0.1)
        for qid in ALL_JUDGE_QUESTIONS
    }
    bad_choice = dict(good)
    bad_choice[JUDGE_PC_AGENCY] = ChoiceResult(
        question_id=JUDGE_PC_AGENCY, selected_id="x",
        probabilities={"x": 1.0}, confidence=1.0,
    )
    with pytest.raises(DecisionError):
        evaluate_judges(bad_choice, deterministic_passed=True)
    bad_prob = dict(good)
    bad_prob[JUDGE_SECRECY_RISK] = NoulResult(
        question_id=JUDGE_SECRECY_RISK, probability=2.0,
    )
    with pytest.raises(DecisionError):
        evaluate_judges(bad_prob, deterministic_passed=True)


# --- shadow mode + calibration ----------------------------------------------


def test_shadow_mode_records_per_role_calibration_rows():
    factory = _setup()
    answers = _clean_answers()
    answers[JUDGE_UNSUPPORTED_ADDITION] = 0.8
    verdict = shadow_judge(
        _service(answers),
        _evidence(),
        deterministic_passed=True,
        session_factory=factory,
        trace_id="trace-384",
        campaign_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
    )
    assert verdict is not None and verdict.directive == JUDGE_REPAIR
    with factory() as db:
        rows = db.query(DecisionTelemetry).all()
        assert len(rows) == 5
        assert {r.mode for r in rows} == {SHADOW}
        assert {r.question_kind for r in rows} == {"noul"}
        assert {r.policy_directive for r in rows} == {JUDGE_REPAIR}
        assert all(r.trace_id == "trace-384" for r in rows)
        by_q = {r.question_id: r for r in rows}
        assert by_q[JUDGE_UNSUPPORTED_ADDITION].selected_id == "violation"
        assert by_q[JUDGE_PC_AGENCY].selected_id == "clean"


def test_shadow_failure_returns_none_without_raising():
    verdict = shadow_judge(
        _service({}),  # no scripted answers: decision call fails
        _evidence(),
        deterministic_passed=True,
        session_factory=None,
    )
    assert verdict is None


def test_judge_records_feed_per_role_calibration():
    verdict, _ = run_judges(
        _service(_clean_answers()), _evidence(), deterministic_passed=True,
    )
    records = build_judge_records(
        verdict, provider="fake-decision", model="fake-decision-model-v1",
        mode=ACTIVE, trace_id="t",
    )
    assert len(records) == 5
    summary = calibration_summary(records)
    assert len(summary) == 5  # one role per judge question, never global
    assert all(role.startswith("semantic_judge/") for role in summary)


def test_record_rows_fail_soft_without_factory():
    verdict, _ = run_judges(
        _service(_clean_answers()), _evidence(), deterministic_passed=True,
    )
    records = build_judge_records(
        verdict, provider="p", model="m", mode=SHADOW,
    )
    assert record_judge_rows(None, records) == []


# --- trace versions ----------------------------------------------------------


def test_trace_carries_versions_without_evidence_text():
    verdict, _ = run_judges(
        _service(_clean_answers()), _evidence(), deterministic_passed=True,
    )
    trace = judge_trace(
        verdict, provider="fake-decision", model="fake-decision-model-v1",
        trace_id="t-1",
    )
    assert trace["question_version"] == JUDGE_SCHEMA_VERSION
    assert trace["policy_version"] == JUDGE_POLICY_VERSION
    assert trace["directive"] == JUDGE_PASS
    blob = str(trace)
    assert "émigré ledger" not in blob
    assert "Lyra studies" not in blob


# --- validator residue -------------------------------------------------------


def test_validator_residue_summary():
    empty = validator_residue_summary(None)
    assert empty == {"failed": False, "codes": [], "categories": []}
    residue = validator_residue_summary(
        [
            {"validator": "canon", "code": "canon_contradiction",
             "category": "canon"},
            {"validator": "agency", "code": "invented_pc_action",
             "category": "agency_violation"},
        ]
    )
    assert residue["failed"] is True
    assert residue["codes"] == ["canon_contradiction", "invented_pc_action"]
    assert residue["categories"] == ["agency_violation", "canon"]
    with pytest.raises(DecisionError):
        validator_residue_summary(["not-a-mapping"])  # type: ignore[list-item]


# --- streaming buffer policy -------------------------------------------------


def test_streaming_never_judges_per_token():
    buffer = BufferedJudgeCheckpoint()
    for _ in range(100):
        buffer.feed("x")  # token-sized deltas never arm a checkpoint
        assert buffer.ready is False
    assert buffer.checkpoints_used == 0


def test_buffered_checkpoint_fires_on_word_boundary():
    buffer = BufferedJudgeCheckpoint()
    buffer.feed("word " * 60)  # 300 chars ending on a boundary
    assert buffer.ready is True
    assert buffer.mark_checkpoint() == 1
    assert buffer.ready is False  # consumed: no new text yet


def test_mid_word_buffer_does_not_fire():
    buffer = BufferedJudgeCheckpoint()
    buffer.feed("word " * 59 + "midwo")
    assert len(buffer.cumulative) >= 240
    assert buffer.ready is False


def test_full_candidate_checkpoint_at_completion():
    policy = StreamJudgePolicy(min_buffer_chars=10000, max_checkpoints=3)
    buffer = BufferedJudgeCheckpoint(policy)
    buffer.feed("short closing line")
    assert buffer.ready is False
    assert buffer.full_candidate_ready is False
    buffer.complete()
    assert buffer.full_candidate_ready is True
    assert buffer.mark_checkpoint() == 1
    assert buffer.full_candidate_ready is False


def test_checkpoint_budget_is_bounded():
    policy = StreamJudgePolicy(min_buffer_chars=10, max_checkpoints=2)
    buffer = BufferedJudgeCheckpoint(policy)
    for _ in range(2):
        buffer.feed("enough text here ")
        assert buffer.ready is True
        buffer.mark_checkpoint()
    buffer.feed("more text here ")
    buffer.complete()
    assert buffer.ready is False
    assert buffer.full_candidate_ready is False
    with pytest.raises(DecisionError):
        buffer.mark_checkpoint()
    with pytest.raises(DecisionError):
        BufferedJudgeCheckpoint().feed(123)  # type: ignore[arg-type]
    with pytest.raises(DecisionError):
        StreamJudgePolicy(min_buffer_chars=0)


def test_subset_judging_for_streaming_checkpoints():
    evidence = _evidence()
    request = build_judge_request(
        evidence, only=(JUDGE_SECRECY_RISK, JUDGE_PC_AGENCY),
    )
    assert [q.question_id for q in request.questions] == [
        JUDGE_SECRECY_RISK, JUDGE_PC_AGENCY,
    ]
    answers = {JUDGE_SECRECY_RISK: 0.1, JUDGE_PC_AGENCY: 0.2}
    verdict, _ = run_judges(
        _service(answers),
        evidence,
        only=(JUDGE_SECRECY_RISK, JUDGE_PC_AGENCY),
        deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_PASS
    assert set(verdict.findings) == {JUDGE_SECRECY_RISK, JUDGE_PC_AGENCY}


def test_regenerate_fallback_for_non_repair_non_safety_questions():
    # Future judge roles outside the safety/repair sets fall back to
    # bounded regeneration while attempts remain, then escalate.
    failing = {"judge_custom": NoulResult(
        question_id="judge_custom", probability=0.9,
    )}
    policy = JudgePolicy(
        fail_thresholds={"judge_custom": 0.5},
    )
    early = evaluate_judges(
        failing, expected_questions=("judge_custom",),
        deterministic_passed=True, attempts_used=0, policy=policy,
    )
    assert early.directive == JUDGE_REGENERATE
    assert judge_retry_allowed(early, attempts_used=0) is True
    late = evaluate_judges(
        failing, expected_questions=("judge_custom",),
        deterministic_passed=True, attempts_used=2, policy=policy,
    )
    assert late.directive == JUDGE_ESCALATE


# --- confidence regression: selected-class probability ------------------------


def test_clean_judge_records_carry_selected_class_confidence():
    verdict, _ = run_judges(
        _service(_clean_answers(probability=0.05)),
        _evidence(),
        deterministic_passed=True,
    )
    assert verdict.directive == JUDGE_PASS
    records = build_judge_records(
        verdict, provider="fake-decision", model="fake-decision-model-v1",
        mode=ACTIVE, trace_id="t",
    )
    for record in records:
        assert record.selected_id == "clean"
        # P(violation)=0.05 means 0.95 confidence in the clean selection.
        assert record.confidence == pytest.approx(0.95)
        assert record.probabilities == {"violation": 0.05, "clean": 0.95}


def test_violation_judge_records_carry_violation_confidence():
    answers = _clean_answers()
    answers[JUDGE_PC_AGENCY] = 0.9
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
    )
    records = build_judge_records(
        verdict, provider="fake-decision", model="fake-decision-model-v1",
        mode=ACTIVE, trace_id="t",
    )
    by_q = {r.question_id: r for r in records}
    assert by_q[JUDGE_PC_AGENCY].selected_id == "violation"
    assert by_q[JUDGE_PC_AGENCY].confidence == pytest.approx(0.9)
    assert by_q[JUDGE_UNSUPPORTED_ADDITION].selected_id == "clean"
    assert by_q[JUDGE_UNSUPPORTED_ADDITION].confidence == pytest.approx(0.95)


# --- narration fidelity shadow wiring ----------------------------------------


def _narration_contract():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract

    return normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "respond",
        "reason": "story continuation",
        "beats": [{
            "id": "beat_1",
            "type": "narration",
            "claims": [{
                "text": "Lyra studies the cracked vault door.",
                "claim_kind": "observation",
                "origin": "dm_adjudication",
                "visibility": "public",
            }],
        }],
    })


def test_narration_shadow_judge_runs_without_changing_deterministic_pass():
    from app.dm.narration import (
        build_narration_judge_evidence,
        check_narration_fidelity_or_raise,
        shadow_judge_narration,
    )

    contract = _narration_contract()
    narration = "Lyra studies the cracked vault door."
    evidence = build_narration_judge_evidence(narration, contract)
    assert evidence.candidate_text.startswith("Lyra studies")
    assert evidence.public_claim_texts == ("Lyra studies the cracked vault door.",)

    factory = _setup()
    verdict = shadow_judge_narration(
        narration,
        contract,
        [],
        judge_service=_service(_clean_answers()),
        judge_session_factory=factory,
        trace_id="trace-384-narr",
    )
    assert verdict is not None and verdict.directive == JUDGE_PASS
    with factory() as db:
        rows = db.query(DecisionTelemetry).all()
        assert len(rows) == 5
        assert {r.mode for r in rows} == {SHADOW}

    # The production gate still passes deterministically with shadow on.
    check_narration_fidelity_or_raise(
        narration,
        contract,
        judge_service=_service(_clean_answers()),
        judge_session_factory=None,
    )


def test_narration_deterministic_failure_stays_final_despite_judge_pass():
    from app.dm.narration import (
        NarrationFidelityError,
        check_narration_fidelity_or_raise,
    )

    contract = _narration_contract()
    # Unsupported number: deterministic fidelity must reject even though the
    # semantic judge is scripted fully clean.
    narration = "Lyra studies the cracked vault door and finds 999 gold."
    with pytest.raises(NarrationFidelityError):
        check_narration_fidelity_or_raise(
            narration,
            contract,
            judge_service=_service(_clean_answers(probability=0.0)),
            judge_session_factory=None,
        )


def test_narration_shadow_failure_never_raises_or_blocks():
    from app.dm.narration import (
        check_narration_fidelity_or_raise,
        shadow_judge_narration,
    )

    contract = _narration_contract()
    narration = "Lyra studies the cracked vault door."
    assert (
        shadow_judge_narration(
            narration,
            contract,
            [],
            judge_service=_service({}),  # unscripted: decision call fails
            judge_session_factory=None,
        )
        is None
    )
    # No service configured: no-op, deterministic pass stands.
    check_narration_fidelity_or_raise(narration, contract)


def test_narration_judge_evidence_includes_non_claim_projection_fields():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    from app.dm.narration import build_narration_judge_evidence

    contract = normalize_contract({
        "contract_version": CONTRACT_VERSION,
        "mode": "await_roll",
        "reason": "uncertain footing",
        "beats": [{
            "id": "beat_1",
            "type": "narration",
            "claims": [{
                "text": "Loose stones cover the ledge ahead.",
                "claim_kind": "roll_instruction",
                "origin": "dm_adjudication",
                "visibility": "public",
            }],
        }],
        "roll_request": {
            "request_id": "roll_1",
            "roll_kind": "check",
            "ability_or_skill": "Acrobatics",
            "label": "Acrobatics check",
            "advantage_state": "normal",
            "reason_public": "The ledge looks treacherous and demands care.",
        },
        "open_player_choice": "What do you do?",
    })
    evidence = build_narration_judge_evidence(
        "Loose stones cover the ledge ahead. The ledge looks treacherous "
        "and demands care. [Acrobatics check] What do you do?",
        contract,
    )
    # The deterministic renderer may emit the roll reason/label and the open
    # choice; the judge must see them as supported, not as additions.
    assert "Loose stones cover the ledge ahead." in evidence.public_claim_texts
    assert "The ledge looks treacherous and demands care." in evidence.public_claim_texts
    assert "Acrobatics check" in evidence.public_claim_texts
    assert "What do you do?" in evidence.public_claim_texts


# --- retry budget follows the active policy -----------------------------------


def test_retry_allowed_follows_non_default_policy_budget():
    answers = _clean_answers()
    answers[JUDGE_CONTRADICTION] = 0.8
    policy = JudgePolicy(max_regenerations=3)
    verdict, _ = run_judges(
        _service(answers), _evidence(), deterministic_passed=True,
        attempts_used=2, policy=policy,
    )
    assert verdict.directive == JUDGE_REPAIR
    assert verdict.max_regenerations == 3
    # The default budget is 2, so a hard-coded default would refuse here.
    assert judge_retry_allowed(verdict, attempts_used=2) is True
    assert judge_retry_allowed(verdict, attempts_used=2, policy=policy) is True
    assert judge_retry_allowed(verdict, attempts_used=3) is False


def test_production_validated_turn_invokes_shadow_judge():
    import uuid as _uuid

    from sqlalchemy.pool import StaticPool

    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    from app.dm.narration import execute_validated_turn

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        from models.campaigns import Campaign
        from models.profiles import Profile
        from models.threads import CampaignThread
        from app.runtime.submissions import accept_submission
        from app.dm.turns import coordinate_turn

        owner = _uuid.uuid4()
        camp_id = _uuid.uuid4()
        thread_id = _uuid.uuid4()
        s.add(Profile(id=owner, email="owner@example.com"))
        s.add(Campaign(id=camp_id, owner_id=owner, name="Table", revision=0))
        s.add(CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner))
        s.commit()
        accept_submission(
            s, campaign_id=camp_id, user_id=_uuid.uuid4(),
            raw_content="I listen at the door.",
            segments=[{"type": "ic", "text": "I listen at the door."}],
            thread_id=str(thread_id),
        )
        s.commit()
        turn, attempt = coordinate_turn(s, camp_id, str(thread_id))
        contract = normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "respond",
            "reason": "story continuation",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{
                    "text": "Silence presses against the oak door.",
                    "claim_kind": "observation", "origin": "dm_adjudication",
                    "visibility": "public",
                }],
            }],
            "open_player_choice": "What do you do?",
        })
        judge_service = _service(_clean_answers())
        out = execute_validated_turn(
            s, turn_id=turn.id, attempt_id=attempt.id, contract=contract,
            publish_realtime=False,
            judge_service=judge_service,
            judge_session_factory=None,
        )
        assert out.narration.completed
        # The production validated-turn path ran the shadow judge call.
        assert len(judge_service.adapter.calls) == 1
