"""Multi-entity identity commit liveness.

A turn that introduces two or more new entities must commit: each stored
pre-narration identity frame is revalidated against one baseline taken
before this commit's own writes, so creating the first entity cannot stale
the second. A commit failure after visible narration must never strand the
turn in ``streaming``, and the dm-execute sweep reclaims any that were.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.decisions import DecisionError, DecisionService
from app.world.identity import (
    NEW_ENTITY,
    promote_new_entities_from_contract,
    resolve_new_entity_identities_pre_narration,
)
from models.dm import DMStream, DmTurn, DmTurnAttempt
from models.threads import PlayerSubmission
from models.world import WorldEntity
from tests.support.fake_decisions import FakeDecisionAdapter
from tests.test_world_identity_214 import (
    _attempt_fake,
    _coordinated_attempt,
    _streaming_setup,
    make_entity,
    setup_db,
)


def _two_proposals(first="Foremost Rider", second="Grey Ferryman"):
    return {"new_entities": [
        {"temp_id": "tmp_npc_1", "kind": "npc", "present": True, "public_name": first},
        {"temp_id": "tmp_npc_2", "kind": "npc", "present": True, "public_name": second},
    ]}


def _two_entity_contract(first="Foremost Rider", second="Grey Ferryman"):
    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "two strangers arrive at the ford",
        "beats": [{
            "id": "beat_1", "type": "narration",
            "claims": [{"text": "A rider and a ferryman wait at the ford.",
                        "claim_kind": "observation", "origin": "established_state",
                        "visibility": "public"}],
        }],
        "new_entities": [
            {"temp_id": "tmp_npc_1", "kind": "npc", "present": True, "public_name": first},
            {"temp_id": "tmp_npc_2", "kind": "npc", "present": True, "public_name": second},
        ],
        "open_player_choice": "What do you do?",
    })


def _no_calls_service():
    return DecisionService(FakeDecisionAdapter(answers={}))


# ── Unit: promotion validates every stored frame against one baseline ────────

def test_two_new_entities_promote_from_stored_frames():
    db, campaign = setup_db()
    snapshot = _two_proposals()
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    outcomes = resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, snapshot, identity_decision_service=_no_calls_service())
    assert [o["outcome"] for o in outcomes] == [NEW_ENTITY, NEW_ENTITY]
    assert all(o["frame"] for o in outcomes)

    service = _no_calls_service()
    promoted = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=service)
    assert [e.name for e in promoted] == ["Foremost Rider", "Grey Ferryman"]
    assert service.adapter.calls == []
    # Idempotent replay returns the same rows.
    again = promote_new_entities_from_contract(
        db, campaign, turn, attempt, identity_decision_service=service)
    assert [e.id for e in again] == [e.id for e in promoted]
    assert db.query(WorldEntity).count() == 2


def test_external_entity_between_resolution_and_commit_is_still_stale():
    db, campaign = setup_db()
    snapshot = _two_proposals()
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    resolve_new_entity_identities_pre_narration(
        db, campaign, turn, attempt, snapshot, identity_decision_service=_no_calls_service())
    # A genuinely concurrent identity write by someone else.
    make_entity(db, campaign, "Rider of the Fen")
    with pytest.raises(DecisionError) as exc:
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=_no_calls_service())
    assert exc.value.kind == "stale"
    assert [row.name for row in db.query(WorldEntity).all()] == ["Rider of the Fen"]


def test_sibling_proposals_with_the_same_name_are_refused_before_narration():
    db, campaign = setup_db()
    snapshot = _two_proposals("Grey Ferryman", "grey  ferryman")
    attempt = _attempt_fake(snapshot)
    turn = type("Turn", (), {"id": uuid.uuid4()})()
    with pytest.raises(ValueError, match="same name"):
        resolve_new_entity_identities_pre_narration(
            db, campaign, turn, attempt, snapshot, identity_decision_service=_no_calls_service())
    assert attempt.identity_resolutions is None
    # Commit-time promotion refuses the same pair too: never two entities.
    with pytest.raises(ValueError, match="same name"):
        promote_new_entities_from_contract(
            db, campaign, turn, attempt, identity_decision_service=_no_calls_service())
    assert db.query(WorldEntity).count() == 0


# ── Liveness: narrated turn with two new entities commits ────────────────────

def test_narrated_turn_with_two_new_entities_commits_and_resolves_submissions():
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    out = execute_validated_turn(
        db, turn_id=turn.id, attempt_id=attempt.id, contract=_two_entity_contract(),
        publish_realtime=False, identity_decision_service=_no_calls_service())
    assert out.turn.status == "succeeded"
    assert out.attempt.status == "succeeded"
    assert sorted(e.name for e in db.query(WorldEntity).all()) == ["Foremost Rider", "Grey Ferryman"]
    subs = db.query(PlayerSubmission).all()
    assert subs and all(s.resolution_status == "resolved" for s in subs)


def test_new_npc_speaking_on_its_introduction_turn_commits():
    from app.dm.contract import CONTRACT_VERSION, normalize_contract
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    contract = normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond",
        "reason": "the rider leader answers",
        "beats": [{
            "id": "beat_1", "type": "npc_dialogue",
            "speaker_temp_id": "tmp_npc_rider", "speaker_public_name": "Masked Rider",
            "truth_status": "truthful",
            "claims": [{"text": "This town owes us a debt.", "claim_kind": "npc_utterance",
                        "origin": "dm_adjudication", "visibility": "public"}],
        }],
        "new_entities": [{"temp_id": "tmp_npc_rider", "kind": "npc", "public_name": "Masked Rider"}],
    })
    out = execute_validated_turn(
        db, turn_id=turn.id, attempt_id=attempt.id, contract=contract,
        publish_realtime=False, identity_decision_service=_no_calls_service())
    assert out.turn.status == "succeeded"
    assert 'Masked Rider says: "This town owes us a debt."' in out.narration.visible_text
    assert [e.name for e in db.query(WorldEntity).all()] == ["Masked Rider"]


def test_post_narration_commit_failure_ends_failed_visible(monkeypatch):
    from app.dm import turns as turns_mod
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)

    def _boom(*args, **kwargs):
        raise RuntimeError("promotion exploded")

    monkeypatch.setattr(turns_mod, "promote_new_entities_from_contract", _boom)
    with pytest.raises(RuntimeError, match="promotion exploded"):
        execute_validated_turn(
            db, turn_id=turn.id, attempt_id=attempt.id, contract=_two_entity_contract(),
            publish_realtime=False, identity_decision_service=_no_calls_service())
    db.expire_all()
    fresh_attempt = db.get(DmTurnAttempt, attempt.id)
    fresh_turn = db.get(DmTurn, turn.id)
    assert fresh_attempt.status == "failed_visible"
    assert fresh_turn.status == "failed_visible"
    assert fresh_attempt.error_class == "commit_failed"
    assert "promotion exploded" in (fresh_attempt.last_error or "")
    assert db.query(WorldEntity).count() == 0


def test_executor_streaming_failure_is_not_dropped():
    """``_record_failure`` marks a still-streaming attempt failed-visible."""
    from app.dm.execution import _record_failure, _Run
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    attempt.status = "streaming"
    turn.status = "streaming"
    db.commit()
    run = _Run.__new__(_Run)
    run.db, run.attempt_id, run.turn_id, run.trace_id = db, attempt.id, turn.id, "t"
    _record_failure(run, RuntimeError("commit blew up"))
    db.expire_all()
    fresh = db.get(DmTurnAttempt, attempt.id)
    assert fresh.status == "failed_visible"
    assert db.get(DmTurn, turn.id).status == "failed_visible"
    assert (fresh.result or {}).get("retryable") is True


# ── Recovery: a stranded streaming attempt is reclaimed by the sweep ─────────

def _strand_streaming(db, campaign, thread_id, *, stream_status="completed", age_seconds=3600):
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    old = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    stream = DMStream(
        id=uuid.uuid4(), campaign_id=campaign.id, thread_id=thread_id,
        turn_id=str(turn.id), attempt_id=str(attempt.id), status=stream_status,
        audience="campaign", updated_at=old,
    )
    db.add(stream)
    db.flush()
    attempt.status = "streaming"
    attempt.stream_id = stream.id
    attempt.updated_at = old
    turn.status = "streaming"
    turn.streaming_attempt_id = attempt.id
    db.commit()
    return turn, attempt


def test_recover_reclaims_streaming_attempt_with_terminal_stream():
    from app.dm.turns import recover_stuck_attempts
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _strand_streaming(db, campaign, thread_id)
    assert recover_stuck_attempts(db, lease_seconds=300) == 1
    db.expire_all()
    fresh = db.get(DmTurnAttempt, attempt.id)
    assert fresh.status == "failed_visible"
    assert fresh.error_class == "stranded_streaming"
    assert db.get(DmTurn, turn.id).status == "failed_visible"
    # Idempotent: nothing left to reclaim.
    assert recover_stuck_attempts(db, lease_seconds=300) == 0


def test_recover_leaves_live_or_fresh_streaming_attempts_alone():
    from app.dm.turns import recover_stuck_attempts
    db, campaign, thread_id = _streaming_setup()
    # Stream still open: narration may still be in flight.
    _turn, attempt = _strand_streaming(db, campaign, thread_id, stream_status="streaming")
    assert recover_stuck_attempts(db, lease_seconds=300) == 0
    assert db.get(DmTurnAttempt, attempt.id).status == "streaming"


def test_recover_leaves_recent_streaming_attempt_alone():
    from app.dm.turns import recover_stuck_attempts
    db, campaign, thread_id = _streaming_setup()
    _turn, attempt = _strand_streaming(db, campaign, thread_id, age_seconds=5)
    assert recover_stuck_attempts(db, lease_seconds=300) == 0
    assert db.get(DmTurnAttempt, attempt.id).status == "streaming"


def test_same_turn_reveal_does_not_stale_new_entity_frames():
    """Staged effects apply before promotion; their identity writes (a
    rename bumps revision and adds an alias) are this commit's, not drift."""
    from app.dm.contract import normalize_contract
    from app.dm.narration import execute_validated_turn
    db, campaign, thread_id = _streaming_setup()
    warder = make_entity(db, campaign, "Hooded Door-Warder")
    db.commit()
    turn, attempt = _coordinated_attempt(db, campaign, thread_id)
    raw = _two_entity_contract().model_dump(mode="json")
    raw["staged_effects"] = [{
        "id": "reveal_1", "effect_type": "reveal_entity_name",
        "arguments": {"entity_id": str(warder.id), "name": "Pell"},
    }]
    out = execute_validated_turn(
        db, turn_id=turn.id, attempt_id=attempt.id, contract=normalize_contract(raw),
        publish_realtime=False, identity_decision_service=DecisionService(FakeDecisionAdapter(
            answers={"resolve_world_entity_identity": NEW_ENTITY})))
    assert out.turn.status == "succeeded"
    names =sorted(e.name for e in db.query(WorldEntity).all())
    assert names == ["Foremost Rider", "Grey Ferryman", "Pell"]


def test_sweep_reclaims_stranded_streaming_turn_and_auto_retries():
    from app.dm.execution import run_dm_execute_sweep
    db, campaign, thread_id = _streaming_setup()
    turn, attempt = _strand_streaming(db, campaign, thread_id)

    def _silent(packet, feedback=None):
        from app.dm.contract import CONTRACT_VERSION, normalize_contract
        return normalize_contract({
            "contract_version": CONTRACT_VERSION, "mode": "silent", "reason": "retry",
        })

    outcome = run_dm_execute_sweep(db, adjudicate=_silent, narrator="deterministic")
    assert outcome["recovered"] == 1
    assert len(outcome["auto_retried"]) == 1
    db.expire_all()
    assert db.get(DmTurnAttempt, attempt.id).status == "abandoned"
    assert db.get(DmTurn, turn.id).current_attempt_id != attempt.id
