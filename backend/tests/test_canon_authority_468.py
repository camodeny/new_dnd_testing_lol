"""Issue #468 — the DM model proposes canon; code confirms it post-turn."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from database import Base  # noqa: E402
import models  # noqa: E402, F401
from app.campaigns.events import commit_campaign_mutation  # noqa: E402
from app.decisions import DecisionService  # noqa: E402
from app.dm.contract import (  # noqa: E402
    CONTRACT_VERSION,
    ContractValidationError,
    contract_json_schema_strict,
    normalize_contract,
)
from app.dm.effects import apply_staged_effects  # noqa: E402
from app.dm.mechanics import canon_supersede_issues  # noqa: E402
from app.post_turn.canon_promotion import (  # noqa: E402
    CONTRADICTS_CANON,
    ESTABLISHED,
    NOT_ESTABLISHED,
    PROMOTION_QUESTION_ID,
    promote_proposed_canon,
)
from app.post_turn.service import run_post_turn_range  # noqa: E402
from app.world.facts import create_fact, list_facts, list_relations  # noqa: E402
from app.world.service import create_entity  # noqa: E402
from models.campaigns import Campaign  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from tests.support.fake_decisions import FakeDecisionAdapter  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auto_trigger(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")


class _NeverCall(FakeDecisionAdapter):
    def execute(self, request, *, model, timeout):
        raise AssertionError("no proposal, so no promotion judgment")


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    db.flush()
    c = Campaign(owner_id=owner, name="canon")
    db.add(c)
    db.commit()
    db.refresh(c)
    return db, c


def _contract(effects, beats=None):
    return {
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "x",
        "beats": beats or [{"id": "beat_1", "type": "narration", "claims": [
            {"text": "The duke is dead.", "claim_kind": "world_fact",
             "origin": "dm_adjudication", "visibility": "public"}]}],
        "staged_effects": effects,
    }


def _turn(db, c, effects, *, beats=None):
    """Commit a turn whose staged effects ran through the real handlers."""
    rev = int(db.get(Campaign, c.id).revision or 0)
    turn = DmTurn(id=uuid.uuid4(), campaign_id=c.id, thread_id="thread-1",
                  source_revision=rev, status="succeeded")
    attempt = DmTurnAttempt(id=uuid.uuid4(), turn_id=turn.id, attempt_number=1,
                            campaign_id=c.id, thread_id="thread-1",
                            source_revision=rev, input_set_revision=0,
                            status="succeeded", staged_effects=effects,
                            contract_snapshot=_contract(effects, beats))
    db.add_all([turn, attempt])
    db.flush()
    apply_staged_effects(db, db.get(Campaign, c.id), effects, turn, attempt)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="dm.turn_resolved",
        payload={"turn_id": str(turn.id), "attempt_id": str(attempt.id),
                 "submission_ids": [], "mode": "respond"},
        visibility="public", operation_id=f"turn-{turn.id}",
    )
    return event


def _proposal(content="The duke is dead.", **extra):
    return {"id": "af1", "effect_type": "assert_fact",
            "arguments": {"content": content, "propose_confirmed": True, **extra}}


def _judge(answer):
    adapter = FakeDecisionAdapter(answers={PROMOTION_QUESTION_ID: answer})
    return adapter, DecisionService(adapter)


# ── A1: the model cannot write or rewrite canon ────────────────────────────

@pytest.mark.parametrize("state,proposes", [
    ("confirmed", True), ("false", False), ("retconned", False)])
@pytest.mark.parametrize("effect_type,args", [
    ("assert_fact", {"content": "The duke is dead."}),
    ("upsert_relation", {"subject_entity_id": "e1", "relation_type": "ally_of",
                         "object_label": "the crown"}),
])
def test_model_canon_states_are_downgraded_not_rejected(state, proposes, effect_type, args):
    contract = normalize_contract(_contract([{"id": "e", "effect_type": effect_type,
                                              "arguments": {**args, "epistemic_state": state}}]))
    stored = contract.model_dump(mode="json")["staged_effects"][0]["arguments"]
    assert stored["epistemic_state"] == "claimed"
    assert stored.get("propose_confirmed", False) is proposes


def test_proposal_on_a_non_claim_is_dropped():
    contract = normalize_contract(_contract([_proposal(epistemic_state="believed")]))
    stored = contract.staged_effects[0].arguments
    assert stored["epistemic_state"] == "believed"
    assert stored["propose_confirmed"] is False


def test_model_confirmed_becomes_a_promotable_claim():
    db, c = _setup()
    contract = normalize_contract(_contract([{"id": "af1", "effect_type": "assert_fact", "arguments": {
        "content": "The duke is dead.", "epistemic_state": "confirmed"}}]))
    event = _turn(db, c, contract.model_dump(mode="json")["staged_effects"])
    assert list_facts(db, c.id)[0].epistemic_state == "claimed"
    _adapter, service = _judge(ESTABLISHED)
    promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    assert list_facts(db, c.id)[0].epistemic_state == "confirmed"


def test_argument_guide_offers_only_model_states():
    guide = contract_json_schema_strict()["$defs"]["StagedEffect"]["properties"]["arguments"]["description"]
    assert "epistemic_state=believed|suspected|claimed|unknown" in guide
    assert "confirmed" not in guide.replace("propose_confirmed", "")
    assert "propose_confirmed" in guide


def test_supersede_of_canon_is_refused_before_narration():
    db, c = _setup()
    canon, _ = create_fact(db, c, content="The duke lives.", epistemic_state="confirmed",
                           visibility="dm_only", operation_id="seed")
    claim, _ = create_fact(db, c, content="Rumor: the duke fled.", visibility="dm_only",
                           operation_id="claim")
    db.flush()
    contract = normalize_contract(_contract([
        {"id": "a", "effect_type": "assert_fact",
         "arguments": {"content": "The duke is dead.", "supersedes_fact_id": str(canon.id)}},
        {"id": "b", "effect_type": "assert_fact",
         "arguments": {"content": "The duke fled north.", "supersedes_fact_id": str(claim.id)}},
        {"id": "c", "effect_type": "assert_fact",
         "arguments": {"content": "x", "supersedes_fact_id": str(uuid.uuid4())}},
    ]))
    issues = canon_supersede_issues(db, c, contract)
    assert [(i.intent_id, i.code) for i in issues] == [("a", "invalid_supersede"), ("c", "invalid_supersede")]
    assert "confirmed canon" in issues[0].message


def test_commit_refuses_supersede_of_canon():
    db, c = _setup()
    canon, _ = create_fact(db, c, content="The duke lives.", epistemic_state="confirmed",
                           visibility="dm_only", operation_id="seed")
    db.flush()
    with pytest.raises(ValueError, match="cannot supersede"):
        _turn(db, c, [{"id": "a", "effect_type": "assert_fact", "arguments": {
            "content": "The duke is dead.", "supersedes_fact_id": str(canon.id)}}])
    assert canon.status == "active"


# ── A2: post-turn promotion ────────────────────────────────────────────────

def test_established_proposal_is_promoted_with_history():
    db, c = _setup()
    event = _turn(db, c, [_proposal()])
    [claim] = list_facts(db, c.id)
    assert claim.epistemic_state == "claimed"
    _adapter, service = _judge(ESTABLISHED)
    out = promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    assert out["tally"][ESTABLISHED] == 1
    [promoted] = list_facts(db, c.id)
    assert promoted.epistemic_state == "confirmed"
    assert promoted.supersedes_id == claim.id
    assert promoted.content == "The duke is dead."
    assert claim.status == "superseded"
    # Replay converges: the promoted row is no longer an active claim.
    again = promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    assert again["tally"]["skipped"] == 1
    assert len(list_facts(db, c.id)) == 1


@pytest.mark.parametrize("answer", [NOT_ESTABLISHED, CONTRADICTS_CANON, "UNCERTAIN"])
def test_rejected_proposal_stays_claimed(answer):
    db, c = _setup()
    event = _turn(db, c, [_proposal()])
    _adapter, service = _judge(answer)
    out = promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    assert out["outcomes"][0]["outcome"] == answer
    [row] = list_facts(db, c.id)
    assert row.epistemic_state == "claimed"


def test_judge_failure_leaves_claim_unconfirmed():
    db, c = _setup()
    event = _turn(db, c, [_proposal()])
    out = promote_proposed_canon(db, db.get(Campaign, c.id), [event],
                                 decision_service=DecisionService(FakeDecisionAdapter(answers={})))
    assert out["outcomes"][0]["outcome"] == "UNCERTAIN"
    assert list_facts(db, c.id)[0].epistemic_state == "claimed"


def test_unproposed_claims_are_never_judged():
    db, c = _setup()
    event = _turn(db, c, [{"id": "af1", "effect_type": "assert_fact",
                           "arguments": {"content": "The innkeeper says the duke is dead."}}])
    out = promote_proposed_canon(db, db.get(Campaign, c.id), [event],
                                 decision_service=DecisionService(_NeverCall()))
    assert out["proposals"] == 0
    assert list_facts(db, c.id)[0].epistemic_state == "claimed"


def test_frame_carries_turn_claims_and_related_canon():
    db, c = _setup()
    duke, _ = create_entity(db, c, entity_type="npc", name="Duke Aldric", operation_id="duke")
    create_fact(db, c, content="Duke Aldric rules Varn.", epistemic_state="confirmed",
                entity_refs=[str(duke.id)], visibility="dm_only", operation_id="canon")
    create_fact(db, c, content="Unrelated canon.", epistemic_state="confirmed",
                visibility="dm_only", operation_id="other")
    db.flush()
    beats = [{"id": "beat_1", "type": "npc_dialogue", "truth_status": "deceptive",
              "dm_private_context": "He lies.",
              "claims": [{"text": "The duke is dead.", "claim_kind": "npc_utterance",
                          "origin": "dm_adjudication", "visibility": "public"}]}]
    event = _turn(db, c, [_proposal(entity_refs=[str(duke.id)])], beats=beats)
    adapter, service = _judge(NOT_ESTABLISHED)
    promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    state = adapter.calls[0]["state"]
    assert state["proposal"]["text"] == "The duke is dead."
    assert state["turn_claims"] == [{"beat_type": "npc_dialogue", "truth_status": "deceptive",
                                     "claim_kind": "npc_utterance", "text": "The duke is dead."}]
    assert [x["text"] for x in state["confirmed_canon"]] == ["Duke Aldric rules Varn."]


def test_relation_proposal_is_promoted():
    db, c = _setup()
    duke, _ = create_entity(db, c, entity_type="npc", name="Duke Aldric", operation_id="duke")
    db.flush()
    event = _turn(db, c, [{"id": "r1", "effect_type": "upsert_relation", "arguments": {
        "subject_entity_id": str(duke.id), "relation_type": "allied_with",
        "object_label": "the Thieves' Guild", "propose_confirmed": True}}])
    adapter, service = _judge(ESTABLISHED)
    promote_proposed_canon(db, db.get(Campaign, c.id), [event], decision_service=service)
    assert adapter.calls[0]["state"]["proposal"]["text"] == "Duke Aldric allied_with the Thieves' Guild"
    [rel] = list_relations(db, c.id)
    assert rel.epistemic_state == "confirmed"


def test_post_turn_range_runs_promotion(monkeypatch):
    import app.post_turn.service as post_turn_service

    staged = []
    monkeypatch.setattr(post_turn_service, "note_authoritative_write",
                        lambda _db, _cid, entries: staged.extend(entries))
    db, c = _setup()
    event = _turn(db, c, [_proposal()])
    [claim] = list_facts(db, c.id)
    _adapter, service = _judge(ESTABLISHED)
    out = run_post_turn_range(db, c.id, event.sequence, event.sequence,
                              clock_decision_service=service)
    assert out["result"]["canon_promotion"]["tally"][ESTABLISHED] == 1
    [promoted] = list_facts(db, c.id)
    assert promoted.epistemic_state == "confirmed"
    # Both versions re-index: the claim's vectors retire, canon's are added.
    assert {("world_fact", claim.id), ("world_fact", promoted.id)} <= set(staged)


# ── B: typed scene patch with an open environment bucket ───────────────────

def _scene(patch):
    return [{"id": "s1", "effect_type": "update_scene",
             "arguments": {"scene_patch": patch, "reason": "moved"}}]


@pytest.mark.parametrize("patch,match", [
    ({"place": "the docks"}, "place"),
    ({"present_actor_names": ["Mira"]}, "present_actor_names"),
    ({"present_actors": [{"name": "Mira"}], "actors_entered": [{"name": "Tom"}]}, "not both"),
    ({"environment": {f"k{i}": i for i in range(33)}}, "at most 32"),
])
def test_invalid_scene_patch_is_rejected(patch, match):
    with pytest.raises(ContractValidationError, match=match):
        normalize_contract(_contract(_scene(patch)))


def test_unknown_scene_key_feeds_regeneration():
    from app.dm.validators import ValidatorPipeline, run_with_bounded_regeneration

    feedback_seen = []

    def adjudicate(packet, feedback):
        feedback_seen.append(feedback)
        patch = {"place": "the docks"} if feedback is None else {"location_name": "the docks"}
        return _contract(_scene(patch))

    contract, report = run_with_bounded_regeneration(
        adjudicate, None, pipeline=ValidatorPipeline([]))
    assert report.passed
    assert contract.staged_effects[0].arguments["scene_patch"] == {"location_name": "the docks"}
    assert feedback_seen[0] is None and "place" in feedback_seen[1]


def test_scene_patch_merges_cast_and_environment():
    from app.world.service import apply_scene_patch, apply_scene_update, get_current_scene

    db, c = _setup()
    apply_scene_update(db, c, new_revision=1, location_name="Ember Gate", fictional_time="dusk",
                       present_actors=[{"name": "Aria", "kind": "pc"}, {"name": "Old Tom"}],
                       environment={"premise": "A siege.", "weather": "ash"})
    apply_scene_patch(db, c, {
        "actors_entered": [{"name": "Mira", "kind": "npc"}],
        "actors_left": ["old tom"],
        "environment": {"weather": None, "ritual_progress": "3/7"},
        "location_name": None,
    }, new_revision=2)
    scene = get_current_scene(db, c.id)
    assert scene.present_actors == [{"name": "Aria", "kind": "pc"}, {"name": "Mira", "kind": "npc"}]
    assert scene.environment == {"premise": "A siege.", "ritual_progress": "3/7"}
    assert scene.location_name is None
    assert scene.fictional_time == "dusk"
