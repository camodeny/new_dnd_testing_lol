"""Issue #217 — post-turn materialization through generated candidates + bounded verification."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, select
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
from app.decisions.adapters.fake import FakeDecisionAdapter  # noqa: E402
from app.post_turn.materialize import (  # noqa: E402
    DEFER,
    MATERIALIZE_QUESTION_ID,
    SUPPORTED,
    UNSUPPORTED,
    MaterializeError,
    extract_candidates,
    materialize_range,
)
from app.post_turn.service import get_checkpoint, run_post_turn_range  # noqa: E402
from app.world.identity import IDENTITY_QUESTION_ID  # noqa: E402
from app.world.knowledge import (  # noqa: E402
    fact_visible_to_viewer,
    list_facts,
    list_relations,
)
from app.world.service import create_entity_inline  # noqa: E402
from models.campaigns import Campaign, CampaignDomainEvent  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


@pytest.fixture(autouse=True)
def _no_auto_trigger(monkeypatch):
    monkeypatch.setenv("POST_TURN_AUTO_TRIGGER", "0")


class _NeverCall(FakeDecisionAdapter):
    def execute(self, request, *, model, timeout):
        raise AssertionError("deterministic materialize path must not call the model")


def _factory():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def _setup():
    F = _factory()
    db = F()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@x.com"))
    db.flush()
    c = Campaign(owner_id=owner, name="materialize")
    db.add(c)
    db.flush()
    db.commit()
    db.refresh(c)
    return F, db, c


def _rev(db, c):
    return int(db.get(Campaign, c.id).revision or 0)


def _commit(db, c, payload=None, etype="game.play", visibility="public", op=None):
    rev = _rev(db, c)
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type=etype, payload=payload or {"n": rev + 1},
        visibility=visibility, operation_id=op or f"op-{rev + 1}-{uuid.uuid4().hex[:6]}",
    )
    return event


def _range(db, c, lo, hi):
    return list(db.execute(select(CampaignDomainEvent).where(
        CampaignDomainEvent.campaign_id == c.id,
        CampaignDomainEvent.sequence >= lo, CampaignDomainEvent.sequence <= hi,
    ).order_by(CampaignDomainEvent.sequence.asc())).scalars().all())


def _scripted(answer):
    return DecisionService(FakeDecisionAdapter(answers={MATERIALIZE_QUESTION_ID: answer}))


def _entity_names(db, c):
    return sorted(e.name for e in db.execute(
        select(WorldEntity).where(WorldEntity.campaign_id == c.id)).scalars().all())


# ── Mechanical compilation: no model calls ─────────────────────────────────

def test_mechanical_entity_relation_fact_without_model_calls():
    _F, db, c = _setup()
    hints = [
        {"category": "entities", "key": "tavern", "visibility": "campaign",
         "data": {"name": "Brindle Tavern", "entity_type": "location",
                  "summary": "A creaky riverside inn."}},
        {"category": "facts", "key": "tavern-open", "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"content": "The Brindle Tavern serves river trout.",
                  "entity_refs": ["Brindle Tavern"]}},
        {"category": "relations", "key": "mira-knows", "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"subject_ref": "Brindle Tavern", "relation_type": "located_in",
                  "object_label": "Riverside"}},
    ]
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": hints})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert out["duplicate"] is False
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 3
    assert mat["applied"] == {"entities": 1, "relations": 1, "facts": 1,
                              "npc_state": 0, "knowledge": 0, "scene": 0,
                              "visibility_grants": 0}
    assert mat["rejected"] == 0 and mat["deferred"] == 0
    assert "Brindle Tavern" in _entity_names(db, c)
    assert len(list_relations(db, c.id)) == 1
    assert len(list_facts(db, c.id)) == 1
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == event.sequence


def test_empty_range_compiles_nothing_and_advances():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    assert extract_candidates([event]) == []
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 0 and mat["applied"] == {
        "entities": 0, "relations": 0, "facts": 0, "npc_state": 0,
        "knowledge": 0, "scene": 0, "visibility_grants": 0}
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == event.sequence


# ── Claims are not promoted to truth ───────────────────────────────────────

def test_player_claim_and_npc_lie_stay_unconfirmed_and_private():
    _F, db, c = _setup()
    hints = [
        {"category": "facts", "key": "claim", "visibility": "campaign",
         "epistemic_state": "claimed",
         "data": {"content": "A PC claims the vault is empty."}},
        {"category": "facts", "key": "lie", "visibility": "dm_only",
         "epistemic_state": "suspected",
         "data": {"content": "The informant lied; the vault is full."}},
    ]
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": hints})
    run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    facts = list_facts(db, c.id)
    by_content = {f.content: f for f in facts}
    assert by_content["A PC claims the vault is empty."].epistemic_state == "claimed"
    lie = by_content["The informant lied; the vault is full."]
    assert lie.epistemic_state == "suspected"
    assert lie.visibility == "dm_only"
    assert fact_visible_to_viewer(lie, False) is False


# ── Duplicate identity routes through #214 ────────────────────────────────

def test_exact_alias_reuses_owner_without_duplicate():
    _F, db, c = _setup()
    from app.world.identity import add_alias
    owner_entity, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-mira", idempotency_key="seed-mira",
    )
    db.flush()
    add_alias(db, owner_entity, "Mira the Red", visibility="campaign")
    db.commit()
    hints = [{"category": "entities", "key": "mira2", "visibility": "campaign",
              "data": {"name": "Mira the Red", "entity_type": "npc", "ref": "Mira the Red"}}]
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": hints})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["outcomes"][0]["outcome"] == "resolved_existing"
    assert mat["outcomes"][0]["entity_id"] == str(owner_entity.id)
    assert _entity_names(db, c).count("Mira the Red") == 0
    assert _entity_names(db, c).count("Mira") == 1


def test_ambiguous_same_name_resolves_through_bounded_identity():
    _F, db, c = _setup()
    existing, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-mira", idempotency_key="seed-mira",
    )
    db.commit()
    hints = [{"category": "entities", "key": "mira2", "visibility": "campaign",
              "data": {"name": "Mira", "entity_type": "npc"}}]
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": hints})
    service = DecisionService(FakeDecisionAdapter(
        answers={IDENTITY_QUESTION_ID: str(existing.id)}))
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence, clock_decision_service=service,
    )
    mat = out["result"]["materialization"]
    assert mat["outcomes"][0]["outcome"] == "resolved_existing"
    assert _entity_names(db, c).count("Mira") == 1


def test_ambiguous_same_name_defers_without_decision_service():
    _F, db, c = _setup()
    _existing, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-mira", idempotency_key="seed-mira",
    )
    db.commit()
    seed_event = _commit(db, c, payload={"n": "seed"})
    events = _range(db, c, seed_event.sequence, seed_event.sequence)
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, seed_event.sequence, seed_event.sequence,
        decision_service=None,
        candidate_provider=lambda evts: (
            [{"category": "entities", "key": "mira2", "visibility": "campaign",
              "mechanical": False, "source_sequence": seed_event.sequence,
              "data": {"name": "Mira", "entity_type": "npc"}}],
            {"role": "test-generator", "model": "test-model"},
        ),
    )
    assert summary["deferred"] == 1
    assert summary["outcomes"][0]["reason"] == "no_decision_service"
    assert _entity_names(db, c).count("Mira") == 1


# ── Bounded verification: reject / defer ───────────────────────────────────

def _generated_fact_candidate(seq):
    return [{"category": "facts", "key": "gen", "visibility": "campaign",
             "mechanical": False, "source_sequence": seq,
             "epistemic_state": "claimed",
             "data": {"content": "The model invents a hidden vault."}}]


def test_generated_candidate_rejected_as_unsupported():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(UNSUPPORTED),
        candidate_provider=lambda evts: (_generated_fact_candidate(event.sequence), {"role": "gen", "model": "m"}),
    )
    assert summary["rejected"] == 1
    assert summary["outcomes"][0]["outcome"] == "rejected"
    assert list_facts(db, c.id) == []
    assert summary["generation"]["generated_candidates"] == 1
    assert summary["verification"]["unsupported"] == 1


def test_generated_candidate_deferred_and_range_still_consumes():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=_scripted(DEFER),
    )
    # No hints: nothing to verify; drive the deferred path directly.
    events = _range(db, c, event.sequence, event.sequence)
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(DEFER),
        candidate_provider=lambda evts: (_generated_fact_candidate(event.sequence), {"role": "gen", "model": "m"}),
    )
    assert summary["deferred"] == 1
    assert list_facts(db, c.id) == []
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == event.sequence


def test_positive_verdict_cannot_override_deterministic_failure():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    bad = [{"category": "relations", "key": "bad", "visibility": "campaign",
            "mechanical": False, "source_sequence": event.sequence,
            "epistemic_state": "confirmed",
            "data": {"subject_ref": "Nobody Here", "relation_type": "knows",
                     "object_label": "Nothing"}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (bad, {"role": "gen", "model": "m"}),
    )
    assert summary["applied"] == {"entities": 0, "relations": 0, "facts": 0,
                                    "npc_state": 0, "knowledge": 0, "scene": 0,
                                    "visibility_grants": 0}
    assert summary["deferred"] == 1
    assert list_relations(db, c.id) == []


# ── Fail-closed compiler output ────────────────────────────────────────────

def test_invalid_hint_fails_run_before_checkpoint():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "portals", "key": "x", "data": {}}]})
    with pytest.raises(Exception):
        run_post_turn_range(
            db, c.id, event.sequence, event.sequence,
            clock_decision_service=DecisionService(_NeverCall()),
        )
    db.rollback()
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == 0


def test_mechanical_dangling_ref_fails_run():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "relations", "key": "dangle", "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"subject_ref": "Ghost Nobody", "relation_type": "knows",
                  "object_label": "Void"}}]})
    with pytest.raises(MaterializeError):
        run_post_turn_range(
            db, c.id, event.sequence, event.sequence,
            clock_decision_service=DecisionService(_NeverCall()),
        )
    db.rollback()
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == 0


# ── Idempotent replay + visible-turn preservation ──────────────────────────

def test_replay_does_not_duplicate_and_preserves_events():
    _F, db, c = _setup()
    hints = [
        {"category": "entities", "key": "tavern", "visibility": "campaign",
         "data": {"name": "Brindle Tavern", "entity_type": "location"}},
        {"category": "facts", "key": "open", "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"content": "The tavern is open.", "entity_refs": ["Brindle Tavern"]}},
    ]
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": hints})
    before = [(e.sequence, e.event_type, e.payload) for e in _range(db, c, 1, event.sequence)]
    first = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert first["duplicate"] is False
    second = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert second["duplicate"] is True
    assert _entity_names(db, c).count("Brindle Tavern") == 1
    assert len(list_facts(db, c.id)) == 1
    after = [(e.sequence, e.event_type, e.payload) for e in _range(db, c, 1, event.sequence)]
    assert before == after


# ── Round-2: committed-structure compiler ──────────────────────────────────

def _turn_event(db, c, staged_effects, *, contract_snapshot=None, visibility="public"):
    """Fabricate a committed turn + attempt, then commit its turn event."""
    from models.dm import DmTurn, DmTurnAttempt
    turn_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    rev = _rev(db, c)
    db.add(DmTurn(id=turn_id, campaign_id=c.id, thread_id="thread-1",
                  source_revision=rev, status="succeeded"))
    db.add(DmTurnAttempt(id=attempt_id, turn_id=turn_id, attempt_number=1,
                         campaign_id=c.id, thread_id="thread-1",
                         source_revision=rev, input_set_revision=0,
                         status="succeeded",
                         staged_effects=staged_effects,
                         contract_snapshot=contract_snapshot or {}))
    db.flush()
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="dm.turn_committed",
        payload={"turn_id": str(turn_id), "attempt_id": str(attempt_id),
                 "submission_ids": [], "mode": "respond"},
        visibility=visibility,
        operation_id=f"turn-{turn_id}",
    )
    return event


def test_normal_turn_record_world_event_materializes_fact():
    _F, db, c = _setup()
    event = _turn_event(db, c, [{
        "id": "rec1", "effect_type": "record_world_event",
        "arguments": {"event_type": "battle", "summary": "The bridge fell at dusk.",
                      "visibility": "public"},
    }])
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 1
    assert mat["applied"]["facts"] == 1
    facts = list_facts(db, c.id)
    assert len(facts) == 1
    assert facts[0].content == "The bridge fell at dusk."
    assert facts[0].epistemic_state == "confirmed"
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == event.sequence


def test_committed_staged_fact_is_not_duplicated():
    """Effects already applied at turn commit are skipped, never recompiled."""
    from app.world.knowledge import create_fact_inline
    _F, db, c = _setup()
    row, _ = create_fact_inline(
        db, c, content="Committed at turn time.", epistemic_state="confirmed",
        visibility="campaign", operation_id="turn-commit",
        idempotency_key="turn-commit-fact",
    )
    db.flush()
    event = _turn_event(db, c, [{
        "id": "af1", "effect_type": "assert_fact",
        "arguments": {"content": "Committed at turn time.",
                      "epistemic_state": "confirmed", "visibility": "campaign"},
    }])
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 0
    assert len(list_facts(db, c.id)) == 1


# ── Round-2: knowledge, scene, and grant categories ────────────────────────

def test_knowledge_acquisition_hint_for_absent_learner():
    """One PC's discovery becomes that character's stance — never party-wide."""
    from app.world.epistemics import list_knowledge_for_subject
    from app.world.knowledge import create_fact_inline
    _F, db, c = _setup()
    hero, _ = create_entity_inline(
        db, c, entity_type="character", name="Ash", visibility="campaign",
        operation_id="seed", idempotency_key="seed-ash")
    scout, _ = create_entity_inline(
        db, c, entity_type="character", name="Bram", visibility="campaign",
        operation_id="seed", idempotency_key="seed-bram")
    fact, _ = create_fact_inline(
        db, c, content="The vault combination is 3-33.", epistemic_state="confirmed",
        visibility="dm_only", operation_id="seed", idempotency_key="seed-fact")
    db.flush()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "knowledge", "key": "ash-knows", "visibility": "dm_only",
         "data": {"subject_kind": "character", "subject_ref": "Ash",
                  "target_kind": "fact", "target_fact_id": str(fact.id),
                  "knowledge_state": "knows",
                  "acquisition_source": "explicit_disclosure"}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["knowledge"] == 1
    assert len(list_knowledge_for_subject(db, c.id, hero.id)) == 1
    assert list_knowledge_for_subject(db, c.id, scout.id) == []


def test_scene_projection_hint_updates_current_scene():
    from app.world.service import get_current_scene
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "scene", "key": "nightfall", "visibility": "campaign",
         "data": {"scene_patch": {"fictional_time": "nightfall",
                                  "location_name": "Brindle Tavern"}}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert out["result"]["materialization"]["applied"]["scene"] == 1
    scene = get_current_scene(db, c.id)
    assert scene.fictional_time == "nightfall"
    assert scene.location_name == "Brindle Tavern"


def test_visibility_grant_hint_authorizes_human_access():
    from app.world.epistemics import has_active_grant
    from app.world.knowledge import create_fact_inline
    from models.campaigns import CampaignMember
    _F, db, c = _setup()
    reader = uuid.uuid4()
    db.add(Profile(id=reader, email="reader@x.com"))
    db.add(CampaignMember(campaign_id=c.id, user_id=reader))
    db.flush()
    fact, _ = create_fact_inline(
        db, c, content="Secret map.", epistemic_state="confirmed",
        visibility="dm_only", operation_id="seed", idempotency_key="seed-map")
    db.flush()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "visibility_grants", "key": "grant-map", "visibility": "dm_only",
         "data": {"target_kind": "fact", "target_ref": str(fact.id),
                  "grantee_user_id": str(reader)}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert out["result"]["materialization"]["applied"]["visibility_grants"] == 1
    assert has_active_grant(db, c.id, "fact", fact.id, reader) is True


# ── Round-2: visibility widening cap ───────────────────────────────────────

def test_private_source_campaign_assertion_fails_run():
    event_holder = {}

    def _setup_private():
        F, db, c = _setup()
        event_holder["event"] = _commit(
            db, c, payload={"n": 1, "post_turn_materialize": [
                {"category": "facts", "key": "leak", "visibility": "campaign",
                 "epistemic_state": "confirmed",
                 "data": {"content": "DM-only secret."}}]},
            visibility="dm_only")
        return F, db, c

    _F, db, c = _setup_private()
    with pytest.raises(MaterializeError):
        run_post_turn_range(
            db, c.id, event_holder["event"].sequence, event_holder["event"].sequence,
            clock_decision_service=DecisionService(_NeverCall()),
        )
    db.rollback()
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == 0
    assert list_facts(db, c.id) == []


def test_reveal_fact_authorizes_disclosure():
    _F, db, c = _setup()
    event = _turn_event(
        db, c,
        [{"id": "rv1", "effect_type": "reveal_fact",
          "arguments": {"item_type": "fact", "item_id": "open-secret",
                        "visibility": "party_known", "reason": "Read aloud."}}],
        visibility="dm_only",
    )
    # Attach the campaign-visible hint to the private turn event.
    payload = dict(event.payload or {})
    payload["post_turn_materialize"] = [
        {"category": "facts", "key": "open-secret", "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"content": "The password is read aloud."}}]
    event.payload = payload
    db.flush()
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["facts"] == 1
    assert mat["rejected"] == 0 and mat["deferred"] == 0


def test_generated_widening_is_rejected_not_applied():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1}, visibility="dm_only")
    events = _range(db, c, event.sequence, event.sequence)
    wide = [{"category": "facts", "key": "wide", "visibility": "campaign",
             "mechanical": False, "source_sequence": event.sequence,
             "epistemic_state": "claimed",
             "data": {"content": "A secret made public."}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (wide, {"role": "gen", "model": "m"}),
    )
    assert summary["rejected"] == 1
    assert summary["outcomes"][0]["reason"].startswith("visibility_widening")
    assert list_facts(db, c.id) == []


# ── Round-2: bounded digest idempotency keys ──────────────────────────────

def test_long_keys_apply_with_bounded_idempotency():
    from app.world.knowledge import create_fact_inline  # noqa: F401
    _F, db, c = _setup()
    long_key = "k" * 128
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "entities", "key": long_key, "visibility": "campaign",
         "data": {"name": "Long Key Tavern", "entity_type": "location"}},
        {"category": "facts", "key": long_key, "visibility": "campaign",
         "epistemic_state": "confirmed",
         "data": {"content": "Long keys still materialize.",
                  "entity_refs": ["Long Key Tavern"]}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"] == {"entities": 1, "relations": 0, "facts": 1,
                              "npc_state": 0, "knowledge": 0, "scene": 0,
                              "visibility_grants": 0}
    assert mat["rejected"] == 0 and mat["deferred"] == 0


# ── Round-3: provider cannot bypass verification ───────────────────────────

def test_provider_mechanical_claim_still_requires_verdict():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    sneaky = [{"category": "facts", "key": "sneaky", "visibility": "campaign",
               "mechanical": True, "source_sequence": event.sequence,
               "epistemic_state": "confirmed",
               "data": {"content": "Provider invents canon."}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(UNSUPPORTED),
        candidate_provider=lambda evts: (sneaky, {"role": "gen", "model": "m"}),
    )
    assert summary["verification"]["decisions"] == 1
    assert summary["rejected"] == 1
    assert list_facts(db, c.id) == []


# ── Round-3: historical-event metadata survives ────────────────────────────

def test_record_world_event_metadata_preserved():
    _F, db, c = _setup()
    event = _turn_event(db, c, [{
        "id": "rec9", "effect_type": "record_world_event",
        "arguments": {"event_type": "oath", "summary": "Mira swore the oath.",
                      "visibility": "public",
                      "payload": {"oath": "protection"},
                      "source_facet_ids": ["facet-1"]},
    }])
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert out["result"]["materialization"]["applied"]["facts"] == 1
    facts = list_facts(db, c.id)
    assert len(facts) == 1
    historical = (facts[0].details or {}).get("historical_event") or {}
    assert historical.get("event_type") == "oath"
    assert historical.get("payload") == {"oath": "protection"}
    assert historical.get("source_facet_ids") == ["facet-1"]
    assert historical.get("effect_id") == "rec9"


# ── Round-3: compiler-created entities carry turn provenance ───────────────

def test_compiler_entity_links_back_to_committed_turn():
    from models.dm import DmTurn
    _F, db, c = _setup()
    turn_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    rev = _rev(db, c)
    db.add(DmTurn(id=turn_id, campaign_id=c.id, thread_id="thread-1",
                  source_revision=rev, status="succeeded"))
    db.flush()
    from models.dm import DmTurnAttempt
    db.add(DmTurnAttempt(id=attempt_id, turn_id=turn_id, attempt_number=1,
                         campaign_id=c.id, thread_id="thread-1",
                         source_revision=rev, input_set_revision=0,
                         status="succeeded", staged_effects=[],
                         contract_snapshot={"new_entities": [{
                             "temp_id": "tmp_npc_1", "kind": "npc",
                             "public_name": "Unpromoted Nina",
                             "public_summary": "Missed by promotion."}]}))
    db.flush()
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="dm.turn_committed",
        payload={"turn_id": str(turn_id), "attempt_id": str(attempt_id),
                 "submission_ids": [], "mode": "respond"},
        operation_id=f"turn-{turn_id}",
    )
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    assert out["result"]["materialization"]["applied"]["entities"] == 1
    names = _entity_names(db, c)
    assert "Unpromoted Nina" in names
    row = db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == c.id,
        WorldEntity.name == "Unpromoted Nina")).scalars().first()
    assert row is not None
    assert row.source_turn_id == turn_id
    assert row.source_attempt_id == attempt_id


# ── Round-4: generated entities are verified too ───────────────────────────

def test_generated_entity_unsupported_is_not_created():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    invented = [{"category": "entities", "key": "invented",
                 "visibility": "campaign",
                 "source_sequence": event.sequence,
                 "data": {"name": "Invented Imp", "entity_type": "npc"}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(UNSUPPORTED),
        candidate_provider=lambda evts: (invented, {"role": "gen", "model": "m"}),
    )
    assert summary["rejected"] == 1
    assert summary["verification"]["unsupported"] == 1
    assert _entity_names(db, c) == []


def test_generated_entity_supported_still_passes_identity_gate():
    _F, db, c = _setup()
    existing, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-mira", idempotency_key="seed-mira")
    db.flush()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    dupe = [{"category": "entities", "key": "dupe", "visibility": "campaign",
             "source_sequence": event.sequence,
             "data": {"name": "Mira", "entity_type": "npc"}}]
    service = DecisionService(FakeDecisionAdapter(answers={
        MATERIALIZE_QUESTION_ID: SUPPORTED,
        IDENTITY_QUESTION_ID: str(existing.id),
    }))
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=service,
        candidate_provider=lambda evts: (dupe, {"role": "gen", "model": "m"}),
    )
    assert summary["verification"]["supported"] == 1
    assert _entity_names(db, c).count("Mira") == 1


# ── Round-4: private evidence cannot launder through a public event ────────

def test_mixed_visibility_range_keeps_private_evidence_private():
    _F, db, c = _setup()
    public_event = _commit(db, c, payload={"n": 1}, visibility="public")
    private_event = _commit(db, c, payload={"n": 2}, visibility="dm_only")
    events = _range(db, c, public_event.sequence, private_event.sequence)
    candidates = [
        {"category": "facts", "key": "pub-fact", "visibility": "campaign",
         "source_sequence": public_event.sequence, "epistemic_state": "claimed",
         "data": {"content": "Public knowledge."}},
        {"category": "facts", "key": "priv-fact", "visibility": "campaign",
         "source_sequence": private_event.sequence, "epistemic_state": "claimed",
         "data": {"content": "Private secret."}},
    ]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events,
        public_event.sequence, private_event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (candidates, {"role": "gen", "model": "m"}),
    )
    assert summary["applied"]["facts"] == 1
    assert summary["rejected"] == 1
    contents = sorted(f.content for f in list_facts(db, c.id))
    assert contents == ["Public knowledge."]


def test_provider_missing_provenance_is_rejected():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    orphan = [{"category": "facts", "key": "orphan", "visibility": "campaign",
               "epistemic_state": "claimed",
               "data": {"content": "No source cited."}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (orphan, {"role": "gen", "model": "m"}),
    )
    assert summary["rejected"] == 1
    assert summary["outcomes"][0]["reason"].startswith("invalid_provenance")
    assert list_facts(db, c.id) == []


# ── Round-5: NPC state materializes with provenance ────────────────────────

def test_npc_state_hint_applies_with_source_marker():
    from app.world.npcs import get_npc_state
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "entities", "key": "arn", "visibility": "campaign",
         "data": {"name": "Arn", "entity_type": "npc"}},
        {"category": "npc_state", "key": "arn-mood", "visibility": "campaign",
         "data": {"entity_ref": "Arn", "current_activity": "Keeping watch.",
                  "disposition": {"mood": "wary"}}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["npc_state"] == 1
    entity = db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == c.id, WorldEntity.name == "Arn")).scalars().first()
    row = get_npc_state(db, c.id, entity.id)
    assert row is not None
    assert row.current_activity == "Keeping watch."
    assert (row.provenance or {}).get("source") == "post_turn_materialize"


def test_two_npc_updates_same_range_both_apply():
    from app.world.npcs import get_npc_state
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "entities", "key": "arn", "visibility": "campaign",
         "data": {"name": "Arn", "entity_type": "npc"}},
        {"category": "npc_state", "key": "first", "visibility": "campaign",
         "data": {"entity_ref": "Arn", "current_activity": "Keeping watch."}},
        {"category": "npc_state", "key": "second", "visibility": "campaign",
         "data": {"entity_ref": "Arn", "current_activity": "Sounding the alarm."}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["npc_state"] == 2
    entity = db.execute(select(WorldEntity).where(
        WorldEntity.campaign_id == c.id, WorldEntity.name == "Arn")).scalars().first()
    row = get_npc_state(db, c.id, entity.id)
    assert row.current_activity == "Sounding the alarm."


# ── Round-5: grants require real provenance ────────────────────────────────

def test_supported_grant_with_missing_provenance_creates_nothing():
    from app.world.epistemics import list_active_grants
    from app.world.knowledge import create_fact_inline
    from models.campaigns import CampaignMember
    _F, db, c = _setup()
    reader = uuid.uuid4()
    db.add(Profile(id=reader, email="reader@x.com"))
    db.add(CampaignMember(campaign_id=c.id, user_id=reader))
    db.flush()
    fact, _ = create_fact_inline(
        db, c, content="Secret map.", epistemic_state="confirmed",
        visibility="dm_only", operation_id="seed", idempotency_key="seed-map")
    db.flush()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    orphan = [{"category": "visibility_grants", "key": "orphan-grant",
               "visibility": "dm_only",
               "data": {"target_kind": "fact", "target_ref": str(fact.id),
                        "grantee_user_id": str(reader)}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (orphan, {"role": "gen", "model": "m"}),
    )
    assert summary["rejected"] == 1
    assert summary["outcomes"][0]["reason"].startswith("invalid_provenance")
    assert list_active_grants(db, c.id, "fact", fact.id) == []


# ── Round-5: same-category writes follow committed chronology ──────────────

def test_scene_updates_apply_in_source_order_not_key_order():
    from app.world.service import get_current_scene
    _F, db, c = _setup()
    first = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "scene", "key": "zulu", "visibility": "campaign",
         "data": {"scene_patch": {"fictional_time": "dawn"}}}]})
    second = _commit(db, c, payload={"n": 2, "post_turn_materialize": [
        {"category": "scene", "key": "alpha", "visibility": "campaign",
         "data": {"scene_patch": {"fictional_time": "dusk"}}}]})
    out = run_post_turn_range(
        db, c.id, first.sequence, second.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["scene"] == 2
    assert get_current_scene(db, c.id).fictional_time == "dusk"


# ── Round-6: verification is bound to cited source evidence ────────────────

def test_verification_sees_only_cited_source_event():
    _F, db, c = _setup()
    public_event = _commit(db, c, payload={"n": 1}, visibility="public")
    private_event = _commit(db, c, payload={"n": 2}, visibility="dm_only")
    events = _range(db, c, public_event.sequence, private_event.sequence)
    candidate = [{"category": "facts", "key": "bound", "visibility": "campaign",
                  "source_sequence": public_event.sequence,
                  "epistemic_state": "claimed",
                  "data": {"content": "Cited public evidence."}}]
    adapter = FakeDecisionAdapter(answers={MATERIALIZE_QUESTION_ID: SUPPORTED})
    summary = materialize_range(
        db, db.get(Campaign, c.id), events,
        public_event.sequence, private_event.sequence,
        decision_service=DecisionService(adapter),
        candidate_provider=lambda evts: (candidate, {"role": "gen", "model": "m"}),
    )
    assert summary["applied"]["facts"] == 1
    assert len(adapter.calls) == 1
    evidence = adapter.calls[0]["state"]["evidence"]
    assert [e["sequence"] for e in evidence] == [public_event.sequence]


# ── Round-7: KEEP_DISTINCT pairs are never triplicated ──────────────────────

def test_promoted_keep_distinct_proposal_is_recognized():
    """A turn-promoted proposal (jit-keyed row) compiles to nothing."""
    from app.world.service import _stable_jit_key
    _F, db, c = _setup()
    first, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed", idempotency_key="seed-mira-1")
    db.flush()
    turn_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    rev = _rev(db, c)
    from models.dm import DmTurn, DmTurnAttempt
    db.add(DmTurn(id=turn_id, campaign_id=c.id, thread_id="thread-1",
                  source_revision=rev, status="succeeded"))
    db.add(DmTurnAttempt(id=attempt_id, turn_id=turn_id, attempt_number=1,
                         campaign_id=c.id, thread_id="thread-1",
                         source_revision=rev, input_set_revision=0,
                         status="succeeded", staged_effects=[],
                         contract_snapshot={"new_entities": [{
                             "temp_id": "tmp_npc_1", "kind": "npc",
                             "public_name": "Mira",
                             "public_summary": "A second Mira."}]}))
    db.flush()
    # Simulate the commit-time KEEP_DISTINCT promotion of that proposal.
    db.add(WorldEntity(
        campaign_id=c.id, entity_type="npc", name="Mira",
        visibility="campaign",
        idempotency_key=_stable_jit_key(attempt_id, "tmp_npc_1")))
    db.flush()
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="dm.turn_committed",
        payload={"turn_id": str(turn_id), "attempt_id": str(attempt_id),
                 "submission_ids": [], "mode": "respond"},
        operation_id=f"turn-{turn_id}")
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 0
    assert _entity_names(db, c).count("Mira") == 2


def test_hint_multi_name_match_defers_through_identity():
    _F, db, c = _setup()
    create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-1", idempotency_key="seed-mira-1")
    create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-2", idempotency_key="seed-mira-2")
    db.flush()
    event = _commit(db, c, payload={"n": 1, "post_turn_materialize": [
        {"category": "entities", "key": "mira3", "visibility": "campaign",
         "data": {"name": "Mira", "entity_type": "npc"}}]})
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["deferred"] == 1
    assert _entity_names(db, c).count("Mira") == 2


# ── Round-8: adventure-completing turns compile too ─────────────────────────

def test_adventure_completed_turn_materializes_record_event():
    _F, db, c = _setup()
    event = _turn_event(db, c, [{
        "id": "recA", "effect_type": "record_world_event",
        "arguments": {"event_type": "victory", "summary": "The dragon fell.",
                      "visibility": "public"},
    }])
    # The commit path promotes adventure-closing turns to this type (#260);
    # the turn/attempt locators survive the promotion.
    event.event_type = "adventure.completed"
    db.flush()
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["applied"]["facts"] == 1
    facts = list_facts(db, c.id)
    assert [f.content for f in facts] == ["The dragon fell."]


# ── Round-8: reuse-then-KEEP_DISTINCT stays two entities ────────────────────

def test_reuse_first_later_keep_distinct_stays_two():
    from models.dm import DmTurn, DmTurnAttempt
    _F, db, c = _setup()
    first, _ = create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed", idempotency_key="seed-mira-1")
    db.flush()
    # Turn 1 reuses Mira (no JIT row stamped); post-turn has not run yet.
    turn_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    rev = _rev(db, c)
    db.add(DmTurn(id=turn_id, campaign_id=c.id, thread_id="thread-1",
                  source_revision=rev, status="succeeded"))
    db.add(DmTurnAttempt(
        id=attempt_id, turn_id=turn_id, attempt_number=1,
        campaign_id=c.id, thread_id="thread-1",
        source_revision=rev, input_set_revision=0,
        status="succeeded", staged_effects=[],
        contract_snapshot={"new_entities": [{
            "temp_id": "tmp_npc_1", "kind": "npc",
            "public_name": "Mira", "public_summary": "Same Mira."}]},
        identity_resolutions=[{
            "temp_id": "tmp_npc_1", "outcome": str(first.id)}]))
    db.flush()
    _c, event = commit_campaign_mutation(
        db, c.id, rev, event_type="dm.turn_committed",
        payload={"turn_id": str(turn_id), "attempt_id": str(attempt_id),
                 "submission_ids": [], "mode": "respond"},
        operation_id=f"turn-{turn_id}")
    # Turn 2 legitimately adds a KEEP_DISTINCT second Mira before
    # post-turn catches up with turn 1.
    create_entity_inline(
        db, c, entity_type="npc", name="Mira", visibility="campaign",
        operation_id="seed-2", idempotency_key="seed-mira-2")
    db.flush()
    out = run_post_turn_range(
        db, c.id, event.sequence, event.sequence,
        clock_decision_service=DecisionService(_NeverCall()),
    )
    mat = out["result"]["materialization"]
    assert mat["proposed"] == 0
    assert _entity_names(db, c).count("Mira") == 2
