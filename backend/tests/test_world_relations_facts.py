"""Issue #210 — durable relations + epistemic facts with provenance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

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
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from app.world.knowledge import (  # noqa: E402
    EPISTEMIC_STATES,
    create_fact_authoritative,
    create_fact_inline,
    create_relation_authoritative,
    create_relation_inline,
    fact_visible_to_viewer,
    filter_facts_for_viewer,
    filter_relations_for_viewer,
    get_relation_strict,
    list_facts,
    list_records_for_source_event,
    list_records_for_source_turn,
    list_relations,
    list_relations_for_entity,
    relation_visible_to_viewer,
    supersede_fact_authoritative,
    supersede_fact_inline,
    supersede_relation_authoritative,
    supersede_relation_inline,
)
from app.world.service import create_entity_authoritative, is_world_authority  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.dm import DMStream, DMStreamChunk  # noqa: E402
from models.profiles import Profile  # noqa: E402


def _engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _setup():
    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner = uuid.uuid4()
    db.add(Profile(id=owner, email="owner@example.com"))
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Knowledge campaign", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.commit()
    db.refresh(camp)
    return Fac, camp.id, owner


def _entities(db, cid, rev):
    mara, _ = create_entity_authoritative(
        db, cid, rev, entity_type="npc", name="Mara",
        operation_id=f"op-mara-{rev}",
    )
    guild, _ = create_entity_authoritative(
        db, cid, rev + 1, entity_type="faction", name="Guild",
        operation_id=f"op-guild-{rev}",
    )
    return mara, guild, rev + 2


def _stream(db, turn, attempt, text="Narration begins."):
    stream = DMStream(
        id=uuid.uuid4(), campaign_id=turn.campaign_id,
        thread_id=uuid.UUID(str(turn.thread_id)),
        turn_id=str(turn.id), attempt_id=str(attempt.id),
        status="streaming", audience=turn.audience,
    )
    db.add(stream)
    db.flush()
    db.add(DMStreamChunk(
        id=uuid.uuid4(), stream_id=stream.id, sequence=0,
        text=text, byte_length=len(text.encode()),
    ))
    stream.first_chunk_at = datetime.now(timezone.utc)
    stream.chunk_count = 1
    db.flush()
    return stream


# ── epistemic vocabulary ────────────────────────────────────────────────────

def test_epistemic_states_cover_required_vocabulary():
    assert {"confirmed", "false", "believed", "suspected", "claimed", "unknown", "retconned"} <= set(EPISTEMIC_STATES)


def test_confirmed_fact_carries_epistemic_state_and_provenance():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    fact, evt = create_fact_authoritative(
        db, cid, rev, content="The bridge collapsed.",
        entity_refs=[mara.id], epistemic_state="confirmed",
        visibility="campaign",
        provenance={"source": "dm_adjudication", "origin": "established_state"},
        operation_id="op-fact-1",
    )
    assert fact.epistemic_state == "confirmed"
    assert fact.status == "active"
    assert fact.provenance["source"] == "dm_adjudication"
    assert fact.entity_refs == [str(mara.id)]
    assert evt.event_type == "world.fact_asserted"
    assert evt.payload["fact_id"] == str(fact.id)


def test_unsupported_player_claim_stays_claim_not_truth():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    claim, _ = create_fact_authoritative(
        db, cid, rev, content="Mara says the vault is unguarded.",
        entity_refs=[mara.id],
        provenance={"source": "player_transcript", "claim_kind": "player_declaration"},
        operation_id="op-claim-1",
    )
    assert claim.epistemic_state == "claimed"
    assert claim.epistemic_state != "confirmed"
    assert len(list_facts(db, cid, epistemic_state="confirmed")) == 0
    assert len(list_facts(db, cid)) == 1


def test_deceptive_npc_statement_stored_as_evidence_not_objective_truth():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    lie, _ = create_fact_authoritative(
        db, cid, rev, content="Mara claims the Guild guards the bridge.",
        entity_refs=[mara.id], epistemic_state="false",
        visibility="dm_only",
        provenance={
            "source": "npc_utterance", "speaker": str(mara.id),
            "truth_status": "deceptive",
            "dm_private_context": "Mara lies to cover the Guild's retreat.",
        },
        operation_id="op-lie-1",
    )
    assert lie.epistemic_state == "false"
    assert lie.visibility == "dm_only"
    # Objective truth is unaffected: no confirmed fact exists.
    assert list_facts(db, cid, epistemic_state="confirmed") == []
    # But the utterance evidence is preserved with its private context.
    assert lie.provenance["truth_status"] == "deceptive"
    assert "retreat" in lie.provenance["dm_private_context"]


# ── relation lifecycle + history ────────────────────────────────────────────

def test_relationship_lifecycle_preserves_historical_evidence():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rival, _ = create_entity_authoritative(
        db, cid, rev, entity_type="faction", name="Rival Guild",
        operation_id="op-rival",
    )
    rev += 1
    rel, evt1 = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign",
        provenance={"source": "dm_adjudication"},
        operation_id="op-rel-1",
    )
    assert evt1.event_type == "world.relation_created"
    assert rel.version == 1
    assert rel.status == "active"

    rel2, evt2 = supersede_relation_authoritative(
        db, cid, rev + 1, rel.id,
        object_entity_id=rival.id, epistemic_state="confirmed",
        provenance={"source": "dm_adjudication", "reason": "Mara defected"},
        operation_id="op-rel-2",
    )
    assert evt2.event_type == "world.relation_superseded"
    assert rel2.version == 2
    assert rel2.status == "active"
    assert str(rel2.supersedes_id) == str(rel.id)
    # History preserved: prior row flipped, never deleted.
    prior = get_relation_strict(db, cid, rel.id)
    assert prior.status == "superseded"
    assert str(prior.superseded_by_id) == str(rel2.id)
    assert prior.object_entity_id == guild.id
    assert rel2.object_entity_id == rival.id
    # Current truth needs no replay: default list shows only the active row.
    assert [str(r.id) for r in list_relations(db, cid)] == [str(rel2.id)]
    assert len(list_relations(db, cid, include_history=True)) == 2


def test_false_to_retconned_supersession():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="false",
        visibility="campaign", operation_id="op-rel-false",
    )
    fixed, _ = supersede_relation_authoritative(
        db, cid, rev + 1, rel.id, epistemic_state="retconned",
        new_status="retracted",
        provenance={"source": "dm_adjudication", "reason": "continuity fix"},
        operation_id="op-rel-retcon",
    )
    assert fixed.epistemic_state == "retconned"
    assert fixed.status == "retracted"
    assert get_relation_strict(db, cid, rel.id).status == "superseded"
    assert list_relations(db, cid) == []

    fact, _ = create_fact_authoritative(
        db, cid, rev + 2, content="The bridge stands.",
        epistemic_state="false", visibility="campaign",
        operation_id="op-fact-false",
    )
    fixed_fact, _ = supersede_fact_authoritative(
        db, cid, rev + 3, fact.id, epistemic_state="retconned",
        new_status="retracted", operation_id="op-fact-retcon",
    )
    assert fixed_fact.epistemic_state == "retconned"
    assert list_facts(db, cid) == []
    assert len(list_facts(db, cid, include_history=True)) == 2


# ── idempotent duplicate retry ──────────────────────────────────────────────

def test_duplicate_retry_creates_no_duplicate_versions():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, evt = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="believed",
        visibility="campaign",
        operation_id="op-rel-dup", idempotency_key="rel-op-1",
    )
    assert evt is not None
    dup, evt2 = create_relation_authoritative(
        db, cid, rev + 1, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign",
        operation_id="op-rel-dup", idempotency_key="rel-op-1",
    )
    assert str(dup.id) == str(rel.id)
    assert dup.epistemic_state == "believed"  # original preserved
    assert evt2 is None
    assert len(list_relations(db, cid, include_history=True)) == 1
    assert db.get(Campaign, cid).revision == rev + 1  # no extra bump

    fact, fevt = create_fact_authoritative(
        db, cid, rev + 1, content="The vault is sealed.",
        epistemic_state="suspected", visibility="campaign",
        operation_id="op-fact-dup", idempotency_key="fact-op-1",
    )
    assert fevt is not None
    fact_dup, fevt2 = create_fact_authoritative(
        db, cid, rev + 2, content="The vault is sealed (retry).",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-fact-dup", idempotency_key="fact-op-1",
    )
    assert str(fact_dup.id) == str(fact.id)
    assert fevt2 is None
    assert len(list_facts(db, cid, include_history=True)) == 1

    # Duplicate supersede retry is equally safe.
    rel2, sevt = supersede_relation_authoritative(
        db, cid, rev + 2, rel.id, epistemic_state="confirmed",
        operation_id="op-rel-sup", idempotency_key="rel-sup-1",
    )
    assert sevt is not None
    rel2_dup, sevt2 = supersede_relation_authoritative(
        db, cid, rev + 3, rel.id, epistemic_state="suspected",
        operation_id="op-rel-sup", idempotency_key="rel-sup-1",
    )
    assert str(rel2_dup.id) == str(rel2.id)
    assert sevt2 is None
    assert len(list_relations(db, cid, include_history=True)) == 2


# ── failure / recovery ──────────────────────────────────────────────────────

def test_failed_supersede_leaves_prior_active_truth_intact():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-ok",
    )
    bogus = uuid.uuid4()
    with pytest.raises(ValueError):
        supersede_relation_authoritative(
            db, cid, rev + 1, rel.id, object_entity_id=bogus,
            operation_id="op-rel-bad",
        )
    db.rollback()
    prior = get_relation_strict(db, cid, rel.id)
    assert prior.status == "active"
    assert prior.epistemic_state == "confirmed"
    assert len(list_relations(db, cid, include_history=True)) == 1
    assert db.get(Campaign, cid).revision == rev + 1


def test_conflicting_identity_reference_fails_closed():
    Fac, cid, owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    # Entity from another campaign must not leak in as a reference.
    other_camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Other", revision=0)
    db.add(other_camp)
    db.flush()
    outsider, _ = create_entity_authoritative(
        db, other_camp.id, 0, entity_type="npc", name="Outsider",
        operation_id="op-outsider",
    )
    with pytest.raises(ValueError):
        create_relation_inline(
            db, db.get(Campaign, cid),
            subject_entity_id=mara.id, relation_type="works_for",
            object_entity_id=outsider.id, operation_id="op-bad-ref",
        )
    db.rollback()
    with pytest.raises(ValueError):
        create_fact_inline(
            db, db.get(Campaign, cid), content="Outsider did it.",
            entity_refs=[outsider.id], operation_id="op-bad-fact-ref",
        )
    db.rollback()
    assert list_relations(db, cid, include_history=True) == []
    assert list_facts(db, cid, include_history=True) == []


def test_superseding_non_active_record_fails():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    supersede_relation_inline(
        db, db.get(Campaign, cid), rel.id, epistemic_state="suspected",
        operation_id="op-r2",
    )
    db.commit()
    with pytest.raises(ValueError):
        supersede_relation_inline(
            db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
            operation_id="op-r3",
        )


# ── lookup by canonical entity / source turn / source event ─────────────────

def test_lookup_by_canonical_entity_and_source_refs():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, rel_evt = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-src",
    )
    fact, fact_evt = create_fact_authoritative(
        db, cid, rev + 1, content="Mara serves the Guild.",
        entity_refs=[mara.id, guild.id], epistemic_state="confirmed",
        visibility="campaign",
        source_event_id=rel_evt.id,
        operation_id="op-fact-src",
    )
    assert fact.source_event_id == rel_evt.id
    # By canonical entity, either side of a relation.
    assert [str(r.id) for r in list_relations_for_entity(db, cid, mara.id)] == [str(rel.id)]
    assert [str(r.id) for r in list_relations_for_entity(db, cid, guild.id)] == [str(rel.id)]
    assert [str(r.id) for r in list_relations(db, cid, subject_entity_id=mara.id)] == [str(rel.id)]
    # Facts referencing an entity resolve through the join table.
    assert [str(f.id) for f in list_facts(db, cid, entity_id=guild.id)] == [str(fact.id)]
    # By source domain event.
    by_event = list_records_for_source_event(db, cid, rel_evt.id)
    assert [str(f.id) for f in by_event["facts"]] == [str(fact.id)]
    # Facts/relations record their source turn.
    turn_id = uuid.uuid4()
    fact2, _ = create_fact_authoritative(
        db, cid, rev + 2, content="Turn-sourced rumor.",
        source_turn_id=turn_id, operation_id="op-fact-turn",
    )
    by_turn = list_records_for_source_turn(db, cid, turn_id)
    assert [str(f.id) for f in by_turn["facts"]] == [str(fact2.id)]
    # Unknown source event fails closed.
    with pytest.raises(ValueError):
        create_fact_authoritative(
            db, cid, rev + 3, content="Bogus provenance.",
            source_event_id=uuid.uuid4(), operation_id="op-fact-bogus",
        )


# ── visibility fail-closed ──────────────────────────────────────────────────

def test_restricted_records_filtered_for_ordinary_member():
    Fac, cid, owner = _setup()
    db = Fac()
    member = uuid.uuid4()
    db.add(Profile(id=member, email="member@example.com"))
    db.add(CampaignMember(campaign_id=cid, user_id=member, role="player"))
    db.commit()
    campaign = db.get(Campaign, cid)
    assert is_world_authority(campaign, owner) is True
    assert is_world_authority(campaign, member) is False
    mara, guild, rev = _entities(db, cid, 0)
    hidden_rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="spies_for",
        object_entity_id=guild.id, visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-hidden-rel",
    )
    # Fail-closed default: unmarked assertions stay restricted.
    default_rel, _ = create_relation_authoritative(
        db, cid, rev + 1, subject_entity_id=mara.id, relation_type="owes",
        object_label="a debt", operation_id="op-default-rel",
    )
    assert default_rel.visibility == "dm_only"
    open_fact, _ = create_fact_authoritative(
        db, cid, rev + 2, content="The market opens at dawn.",
        visibility="campaign", operation_id="op-open-fact",
    )
    assert relation_visible_to_viewer(hidden_rel, True) is True
    assert relation_visible_to_viewer(hidden_rel, False) is False
    assert [str(r.id) for r in filter_relations_for_viewer(list_relations(db, cid, include_history=True), False)] == []
    assert [str(f.id) for f in filter_facts_for_viewer(list_facts(db, cid), False)] == [str(open_fact.id)]
    assert fact_visible_to_viewer(open_fact, False) is True


# ── staged effects + post-turn atomicity ────────────────────────────────────

def _commit_knowledge_turn(db, cid, owner, tid, staged_effects):
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="The DM speaks",
        segments=[{"type": "ic", "text": "The DM speaks."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.staged_effects = staged_effects
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    return commit_turn(db, turn.id, attempt.id)


def test_staged_knowledge_effects_commit_atomically_with_turn():
    Fac, cid, owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    _t, _a, event = _commit_knowledge_turn(db, cid, owner, tid, [
        {"id": "eff-rel-1", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(mara.id), "relation_type": "works_for",
            "object_entity_id": str(guild.id), "epistemic_state": "confirmed",
            "visibility": "campaign",
        }},
        {"id": "eff-fact-1", "effect_type": "assert_fact", "arguments": {
            "content": "Mara serves the Guild openly.",
            "entity_refs": [str(mara.id), str(guild.id)],
            "epistemic_state": "confirmed", "visibility": "campaign",
        }},
    ])
    assert event is not None
    rels = list_relations(db, cid)
    facts = list_facts(db, cid)
    assert len(rels) == 1 and rels[0].relation_type == "works_for"
    assert rels[0].source_attempt_id is not None
    assert len(facts) == 1 and facts[0].epistemic_state == "confirmed"
    # Duplicate commit replay (same operation) stages nothing new.
    _t2, _a2, event2 = commit_turn(db, _t.id, _a.id)
    assert str(event2.id) == str(event.id)
    assert len(list_relations(db, cid, include_history=True)) == 1
    assert len(list_facts(db, cid, include_history=True)) == 1


def test_failed_staged_knowledge_effect_rolls_back_whole_turn():
    Fac, cid, owner = _setup()
    db = Fac()
    mara, _, _rev = _entities(db, cid, 0)
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Bad effect",
        segments=[{"type": "ic", "text": "Bad effect."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.staged_effects = [
        {"id": "eff-good", "effect_type": "assert_fact", "arguments": {
            "content": "This should not survive.", "visibility": "campaign",
        }},
        {"id": "eff-bad", "effect_type": "upsert_relation", "arguments": {
            # Conflicting reference: no such object entity.
            "subject_entity_id": str(mara.id), "relation_type": "works_for",
            "object_entity_id": str(uuid.uuid4()),
        }},
    ]
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    with pytest.raises(ValueError):
        commit_turn(db, turn.id, attempt.id)
    db.rollback()
    assert list_facts(db, cid, include_history=True) == []
    assert list_relations(db, cid, include_history=True) == []


def test_private_attempt_rejects_public_knowledge_effect():
    from app.dm.effects import apply_staged_effects

    Fac, cid, _owner = _setup()
    db = Fac()
    campaign = db.get(Campaign, cid)
    turn = SimpleNamespace(id=uuid.uuid4())
    attempt = SimpleNamespace(id=uuid.uuid4(), audience="private", commit_operation_id="op-x")
    with pytest.raises(ValueError):
        apply_staged_effects(db, campaign, [
            {"id": "eff-pub", "effect_type": "assert_fact", "arguments": {
                "content": "Leak?", "visibility": "public",
            }},
        ], turn, attempt)


def test_contract_validates_new_knowledge_effect_types():
    from app.dm.contract import normalize_contract

    raw = {
        "contract_version": "dm_turn_contract_v1",
        "mode": "respond", "reason": "knowledge update",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "Mara nods.", "claim_kind": "observation",
            "origin": "dm_adjudication",
        }]}],
        "staged_effects": [
            {"id": "eff-f1", "effect_type": "assert_fact", "arguments": {
                "content": "Mara serves the Guild.",
                "epistemic_state": "believed", "visibility": "campaign",
            }},
            {"id": "eff-r1", "effect_type": "upsert_relation", "arguments": {
                "subject_entity_id": str(uuid.uuid4()), "relation_type": "works_for",
                "object_label": "the Guild",
            }},
        ],
    }
    contract = normalize_contract(raw)
    assert [e.effect_type for e in contract.staged_effects] == ["assert_fact", "upsert_relation"]


# ── review round 1: regression tests ────────────────────────────────────────

def test_concurrent_fact_insert_loser_leaves_winner_refs_intact():
    from unittest import mock

    import app.world.knowledge as knowledge
    from app.world.knowledge import _find_fact_by_idempotency
    from models.world import WorldFactEntityRef

    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    campaign = db.get(Campaign, cid)
    winner, created = create_fact_inline(
        db, campaign, content="Mara serves the Guild.",
        entity_refs=[mara.id], epistemic_state="confirmed",
        visibility="campaign", idempotency_key="fact-race-1",
    )
    assert created is True
    db.commit()

    # Simulate the race window: the precheck SELECT misses, the upsert
    # absorbs the unique conflict, and the post-insert lookup finds the
    # winner. The loser references a DIFFERENT entity.
    real_find = _find_fact_by_idempotency
    calls = {"n": 0}

    def flaky_find(db_, cid_, key_):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(db_, cid_, key_)

    with mock.patch.object(knowledge, "_find_fact_by_idempotency", side_effect=flaky_find):
        loser, created2 = create_fact_inline(
            db, db.get(Campaign, cid), content="Guild owns Mara (loser).",
            entity_refs=[guild.id], epistemic_state="suspected",
            visibility="campaign", idempotency_key="fact-race-1",
        )
    assert created2 is False
    assert str(loser.id) == str(winner.id)
    # Winner payload untouched and its lookup index still matches it.
    assert loser.content == "Mara serves the Guild."
    assert loser.entity_refs == [str(mara.id)]
    ref_rows = db.execute(
        select(WorldFactEntityRef).where(WorldFactEntityRef.fact_id == winner.id)
    ).scalars().all()
    assert [str(r.entity_id) for r in ref_rows] == [str(mara.id)]
    db.commit()
    assert len(list_facts(db, cid, include_history=True)) == 1


def test_inline_supersede_retry_after_commit_returns_existing_version():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    new, created = supersede_relation_inline(
        db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
        operation_id="op-r-sup",
    )
    assert created is True
    db.commit()
    # Exact retry after the prior flipped to superseded: idempotent, no raise.
    same, created2 = supersede_relation_inline(
        db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
        operation_id="op-r-sup",
    )
    assert created2 is False
    assert str(same.id) == str(new.id)
    db.commit()

    fact, _ = create_fact_authoritative(
        db, cid, rev + 1, content="The vault is sealed.",
        operation_id="op-f1",
    )
    new_fact, fcreated = supersede_fact_inline(
        db, db.get(Campaign, cid), fact.id, epistemic_state="confirmed",
        operation_id="op-f-sup",
    )
    assert fcreated is True
    db.commit()
    same_fact, fcreated2 = supersede_fact_inline(
        db, db.get(Campaign, cid), fact.id, epistemic_state="confirmed",
        operation_id="op-f-sup",
    )
    assert fcreated2 is False
    assert str(same_fact.id) == str(new_fact.id)


def test_supersede_idempotency_key_collision_fails_closed():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    campaign = db.get(Campaign, cid)
    rel_a, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-ra",
    )
    rel_b, _ = create_relation_authoritative(
        db, cid, rev + 1, subject_entity_id=mara.id, relation_type="owes",
        object_label="a debt", operation_id="op-rb",
    )
    supersede_relation_inline(
        db, campaign, rel_a.id, epistemic_state="confirmed",
        operation_id="op-shared-key",
    )
    db.commit()
    # Same key reused against a DIFFERENT prior: fail closed, not mislinked.
    with pytest.raises(ValueError):
        supersede_relation_inline(
            db, db.get(Campaign, cid), rel_b.id, epistemic_state="confirmed",
            operation_id="op-shared-key",
        )
    db.rollback()
    assert get_relation_strict(db, cid, rel_b.id).status == "active"


def test_new_version_status_superseded_is_rejected():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = create_relation_authoritative(
        db, cid, rev, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    with pytest.raises(ValueError):
        supersede_relation_inline(
            db, db.get(Campaign, cid), rel.id, new_status="superseded",
            operation_id="op-r-bad",
        )
    db.rollback()
    fact, _ = create_fact_authoritative(
        db, cid, rev + 1, content="The vault is sealed.",
        operation_id="op-f1",
    )
    with pytest.raises(ValueError):
        supersede_fact_inline(
            db, db.get(Campaign, cid), fact.id, new_status="superseded",
            operation_id="op-f-bad",
        )
    db.rollback()
    assert get_relation_strict(db, cid, rel.id).status == "active"
    assert len(list_relations(db, cid, include_history=True)) == 1
    assert len(list_facts(db, cid, include_history=True)) == 1


def test_overlong_object_label_rejected_not_truncated():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    campaign = db.get(Campaign, cid)
    long_label = "x" * 257
    with pytest.raises(ValueError):
        create_relation_inline(
            db, campaign, subject_entity_id=mara.id, relation_type="owes",
            object_label=long_label, operation_id="op-long",
        )
    db.rollback()
    # 256-char boundary is accepted verbatim.
    ok_label = "y" * 256
    rel, created = create_relation_inline(
        db, db.get(Campaign, cid), subject_entity_id=mara.id,
        relation_type="owes", object_label=ok_label, operation_id="op-ok",
    )
    assert created is True
    assert rel.object_label == ok_label
    db.commit()
    with pytest.raises(ValueError):
        supersede_relation_inline(
            db, db.get(Campaign, cid), rel.id, object_label=long_label,
            operation_id="op-long-sup",
        )
    db.rollback()
    assert get_relation_strict(db, cid, rel.id).object_label == ok_label


def test_long_operation_id_keeps_same_type_effects_distinct():
    from app.dm.effects import _default_effect_key

    Fac, cid, owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    # Unit level: derived keys stay bounded and distinct per effect.
    attempt_ns = SimpleNamespace(id=uuid.uuid4())
    key_a = _default_effect_key(attempt_ns, {"id": "eff-fact-a"})
    key_b = _default_effect_key(attempt_ns, {"id": "eff-fact-b"})
    assert key_a != key_b
    assert len(key_a) <= 128 and len(key_b) <= 128

    # Turn level: a max-length commit_operation_id must not collapse two
    # same-type effects into one durable key (the second write would be
    # silently skipped as a false duplicate while the turn reports success).
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Two rumors",
        segments=[{"type": "ic", "text": "Two rumors."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.commit_operation_id = "x" * 128
    attempt.staged_effects = [
        {"id": "eff-fact-a", "effect_type": "assert_fact", "arguments": {
            "content": "First rumor.", "epistemic_state": "claimed",
            "visibility": "campaign",
        }},
        {"id": "eff-fact-b", "effect_type": "assert_fact", "arguments": {
            "content": "Second rumor.", "epistemic_state": "claimed",
            "visibility": "campaign",
        }},
        {"id": "eff-rel-a", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(mara.id), "relation_type": "knows",
            "object_entity_id": str(guild.id), "visibility": "campaign",
        }},
        {"id": "eff-rel-b", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(mara.id), "relation_type": "owes",
            "object_label": "a debt", "visibility": "campaign",
        }},
    ]
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    _t, _a, event = commit_turn(db, turn.id, attempt.id)
    assert event is not None
    facts = sorted(list_facts(db, cid), key=lambda f: f.content)
    assert [f.content for f in facts] == ["First rumor.", "Second rumor."]
    assert len({f.idempotency_key for f in facts}) == 2
    relations = sorted(list_relations(db, cid), key=lambda r: r.relation_type)
    assert [r.relation_type for r in relations] == ["knows", "owes"]
    assert len({r.idempotency_key for r in relations}) == 2


def test_duplicate_staged_effect_ids_rejected_before_commit():
    from app.dm.contract import ContractValidationError, normalize_contract
    from app.dm.turns import stage_validated_attempt

    raw = {
        "contract_version": "dm_turn_contract_v1",
        "mode": "respond", "reason": "dup ids",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "Mara nods.", "claim_kind": "observation",
            "origin": "dm_adjudication",
        }]}],
        "staged_effects": [
            {"id": "eff-1", "effect_type": "assert_fact", "arguments": {
                "content": "First rumor.",
            }},
            {"id": "eff-1", "effect_type": "assert_fact", "arguments": {
                "content": "Second rumor.",
            }},
        ],
    }
    with pytest.raises(ContractValidationError):
        normalize_contract(raw)

    # The staging layer guards the raw-dict path too.
    Fac, cid, owner = _setup()
    db = Fac()
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Dup",
        segments=[{"type": "ic", "text": "Dup."}], thread_id=tid,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, cid, tid)
    with pytest.raises(ValueError, match="unique"):
        stage_validated_attempt(db, attempt.id, {
            "contract_version": "dm_turn_contract_v1",
            "staged_effects": [
                {"id": "eff-1", "effect_type": "assert_fact",
                 "arguments": {"content": "First rumor."}},
                {"id": "eff-1", "effect_type": "assert_fact",
                 "arguments": {"content": "Second rumor."}},
            ],
        })
    db.rollback()


@pytest.fixture
def knowledge_api(monkeypatch):
    from fastapi.testclient import TestClient

    from app.auth.service import TEST_USER_ID
    from database import get_db
    from main import app

    eng = _engine()
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    owner = TEST_USER_ID
    member = uuid.uuid4()
    cid = uuid.uuid4()
    db = Fac()
    db.add(Profile(id=owner, email="owner@example.com"))
    db.add(Profile(id=member, email="member@example.com"))
    db.add(Campaign(id=cid, owner_id=owner, name="Knowledge campaign", revision=0))
    db.flush()
    db.add(CampaignMember(campaign_id=cid, user_id=owner, role="owner"))
    db.add(CampaignMember(campaign_id=cid, user_id=member, role="player"))
    db.commit()
    db.close()

    def override_db():
        session = Fac()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(
        "app.world.router.resolve_profile",
        lambda req, db: db.get(Profile, TEST_USER_ID),
    )
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), cid, owner, member
    finally:
        app.dependency_overrides.clear()


def test_knowledge_reads_are_viewer_aware_over_http(knowledge_api, monkeypatch):
    client, cid, owner, member = knowledge_api
    base = f"/api/campaigns/{cid}/world"
    r = client.post(f"{base}/entities", json={
        "expected_revision": 0, "entity_type": "npc", "name": "Mara",
        "operation_id": "op-mara",
    }, headers={"Idempotency-Key": "op-mara"})
    assert r.status_code == 200, r.text
    mara_id = r.json()["entity"]["id"]
    r = client.post(f"{base}/entities", json={
        "expected_revision": 1, "entity_type": "faction", "name": "Guild",
        "operation_id": "op-guild",
    }, headers={"Idempotency-Key": "op-guild"})
    assert r.status_code == 200, r.text
    guild_id = r.json()["entity"]["id"]
    # Public relation + restricted fact.
    r = client.post(f"{base}/relations", json={
        "expected_revision": 2, "subject_entity_id": mara_id,
        "relation_type": "works_for", "object_entity_id": guild_id,
        "epistemic_state": "confirmed", "visibility": "campaign",
        "operation_id": "op-rel",
    }, headers={"Idempotency-Key": "op-rel"})
    assert r.status_code == 200, r.text
    rel_id = r.json()["relation"]["id"]
    r = client.post(f"{base}/facts", json={
        "expected_revision": 3, "content": "Mara lies to the party.",
        "entity_refs": [mara_id], "epistemic_state": "false",
        "visibility": "dm_only", "operation_id": "op-fact",
    }, headers={"Idempotency-Key": "op-fact"})
    assert r.status_code == 200, r.text
    fact_id = r.json()["fact"]["id"]
    # Owner sees everything.
    assert len(client.get(f"{base}/relations").json()["relations"]) == 1
    assert len(client.get(f"{base}/facts").json()["facts"]) == 1
    # Ordinary member: public relation visible, dm_only fact hidden as 404.
    monkeypatch.setattr(
        "app.world.router.resolve_profile",
        lambda req, db: db.get(Profile, member),
    )
    assert [x["id"] for x in client.get(f"{base}/relations").json()["relations"]] == [rel_id]
    assert client.get(f"{base}/facts").json()["facts"] == []
    assert client.get(f"{base}/facts/{fact_id}").status_code == 404
    assert client.get(f"{base}/relations/{rel_id}").status_code == 200
