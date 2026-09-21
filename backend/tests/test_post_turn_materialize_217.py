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
    assert mat["applied"] == {"entities": 1, "relations": 1, "facts": 1, "npc_state": 0}
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
        "entities": 0, "relations": 0, "facts": 0, "npc_state": 0}
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
    events = _range(db, c, 1, _rev(db, c))
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, 1, _rev(db, c),
        decision_service=None,
        candidate_provider=lambda evts: (
            [{"category": "entities", "key": "mira2", "visibility": "campaign",
              "mechanical": False,
              "data": {"name": "Mira", "entity_type": "npc"}}],
            {"role": "test-generator", "model": "test-model"},
        ),
    )
    assert summary["deferred"] == 1
    assert summary["outcomes"][0]["reason"] == "duplicate_identity_no_decision_service"
    assert _entity_names(db, c).count("Mira") == 1


# ── Bounded verification: reject / defer ───────────────────────────────────

def _generated_fact_candidate():
    return [{"category": "facts", "key": "gen", "visibility": "campaign",
             "mechanical": False, "epistemic_state": "claimed",
             "data": {"content": "The model invents a hidden vault."}}]


def test_generated_candidate_rejected_as_unsupported():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(UNSUPPORTED),
        candidate_provider=lambda evts: (_generated_fact_candidate(), {"role": "gen", "model": "m"}),
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
        candidate_provider=lambda evts: (_generated_fact_candidate(), {"role": "gen", "model": "m"}),
    )
    assert summary["deferred"] == 1
    assert list_facts(db, c.id) == []
    assert get_checkpoint(db, c.id, commit=False).processed_through_sequence == event.sequence


def test_positive_verdict_cannot_override_deterministic_failure():
    _F, db, c = _setup()
    event = _commit(db, c, payload={"n": 1})
    events = _range(db, c, event.sequence, event.sequence)
    bad = [{"category": "relations", "key": "bad", "visibility": "campaign",
            "mechanical": False, "epistemic_state": "confirmed",
            "data": {"subject_ref": "Nobody Here", "relation_type": "knows",
                     "object_label": "Nothing"}}]
    summary = materialize_range(
        db, db.get(Campaign, c.id), events, event.sequence, event.sequence,
        decision_service=_scripted(SUPPORTED),
        candidate_provider=lambda evts: (bad, {"role": "gen", "model": "m"}),
    )
    assert summary["applied"] == {"entities": 0, "relations": 0, "facts": 0, "npc_state": 0}
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
