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
from tests.support.world_writes import commit_world_write  # noqa: E402
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.submissions.service import accept_submission  # noqa: E402
from app.threads.service import get_or_create_campaign_thread  # noqa: E402
from app.world.facts import (  # noqa: E402
    EPISTEMIC_STATES,
    create_fact,
    create_relation,
    get_relation_strict,
    list_facts,
    list_records_for_source_turn,
    list_relations,
    supersede_fact,
    supersede_relation,
)
from app.visibility.access import may_user_receive  # noqa: E402
from app.world.service import create_entity  # noqa: E402
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
    mara, _ = commit_world_write(
        db, cid, rev, create_entity, entity_type="npc", name="Mara",
        operation_id=f"op-mara-{rev}",
    )
    guild, _ = commit_world_write(
        db, cid, rev + 1, create_entity, entity_type="faction", name="Guild",
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
    fact, evt = commit_world_write(
        db, cid, rev, create_fact, content="The bridge collapsed.",
        entity_refs=[mara.id], epistemic_state="confirmed",
        visibility="campaign",
        provenance={"source": "dm_adjudication", "origin": "established_state"},
        operation_id="op-fact-1",
    )
    assert fact.epistemic_state == "confirmed"
    assert fact.status == "active"
    assert fact.provenance["source"] == "dm_adjudication"
    assert fact.entity_refs == [str(mara.id)]


def test_unsupported_player_claim_stays_claim_not_truth():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, _, rev = _entities(db, cid, 0)
    claim, _ = commit_world_write(
        db, cid, rev, create_fact, content="Mara says the vault is unguarded.",
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
    lie, _ = commit_world_write(
        db, cid, rev, create_fact, content="Mara claims the Guild guards the bridge.",
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
    rival, _ = commit_world_write(
        db, cid, rev, create_entity, entity_type="faction", name="Rival Guild",
        operation_id="op-rival",
    )
    rev += 1
    rel, evt1 = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign",
        provenance={"source": "dm_adjudication"},
        operation_id="op-rel-1",
    )
    assert rel.version == 1
    assert rel.status == "active"

    rel2, evt2 = commit_world_write(
        db, cid, rev + 1, supersede_relation, rel.id,
        object_entity_id=rival.id, epistemic_state="confirmed",
        provenance={"source": "dm_adjudication", "reason": "Mara defected"},
        operation_id="op-rel-2",
    )
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
    rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="false",
        visibility="campaign", operation_id="op-rel-false",
    )
    fixed, _ = commit_world_write(
        db, cid, rev + 1, supersede_relation, rel.id, epistemic_state="retconned",
        new_status="retracted",
        provenance={"source": "dm_adjudication", "reason": "continuity fix"},
        operation_id="op-rel-retcon",
    )
    assert fixed.epistemic_state == "retconned"
    assert fixed.status == "retracted"
    assert get_relation_strict(db, cid, rel.id).status == "superseded"
    assert list_relations(db, cid) == []

    fact, _ = commit_world_write(
        db, cid, rev + 2, create_fact, content="The bridge stands.",
        epistemic_state="false", visibility="campaign",
        operation_id="op-fact-false",
    )
    fixed_fact, _ = commit_world_write(
        db, cid, rev + 3, supersede_fact, fact.id, epistemic_state="retconned",
        new_status="retracted", operation_id="op-fact-retcon",
    )
    assert fixed_fact.epistemic_state == "retconned"
    assert list_facts(db, cid) == []
    assert len(list_facts(db, cid, include_history=True)) == 2


# ── idempotent duplicate retry ──────────────────────────────────────────────

def test_duplicate_retry_creates_no_duplicate_versions():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    campaign = db.get(Campaign, cid)
    rel, created = create_relation(
        db, campaign, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="believed",
        visibility="campaign",
        operation_id="op-rel-dup", idempotency_key="rel-op-1",
    )
    assert created is True
    dup, created_again = create_relation(
        db, campaign, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign",
        operation_id="op-rel-dup", idempotency_key="rel-op-1",
    )
    assert str(dup.id) == str(rel.id)
    assert dup.epistemic_state == "believed"  # original preserved
    assert created_again is False
    assert len(list_relations(db, cid, include_history=True)) == 1

    fact, created = create_fact(
        db, campaign, content="The vault is sealed.",
        epistemic_state="suspected", visibility="campaign",
        operation_id="op-fact-dup", idempotency_key="fact-op-1",
    )
    assert created is True
    fact_dup, created_again = create_fact(
        db, campaign, content="The vault is sealed (retry).",
        epistemic_state="confirmed", visibility="campaign",
        operation_id="op-fact-dup", idempotency_key="fact-op-1",
    )
    assert str(fact_dup.id) == str(fact.id)
    assert created_again is False
    assert len(list_facts(db, cid, include_history=True)) == 1

    # Duplicate supersede retry is equally safe.
    rel2, created = supersede_relation(
        db, campaign, rel.id, epistemic_state="confirmed",
        operation_id="op-rel-sup", idempotency_key="rel-sup-1",
    )
    assert created is True
    rel2_dup, created_again = supersede_relation(
        db, campaign, rel.id, epistemic_state="suspected",
        operation_id="op-rel-sup", idempotency_key="rel-sup-1",
    )
    assert str(rel2_dup.id) == str(rel2.id)
    assert created_again is False
    assert len(list_relations(db, cid, include_history=True)) == 2


# ── failure / recovery ──────────────────────────────────────────────────────

def test_failed_supersede_leaves_prior_active_truth_intact():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-ok",
    )
    bogus = uuid.uuid4()
    with pytest.raises(ValueError):
        commit_world_write(
            db, cid, rev + 1, supersede_relation, rel.id, object_entity_id=bogus,
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
    outsider, _ = commit_world_write(
        db, other_camp.id, 0, create_entity, entity_type="npc", name="Outsider",
        operation_id="op-outsider",
    )
    with pytest.raises(ValueError):
        create_relation(
            db, db.get(Campaign, cid),
            subject_entity_id=mara.id, relation_type="works_for",
            object_entity_id=outsider.id, operation_id="op-bad-ref",
        )
    db.rollback()
    with pytest.raises(ValueError):
        create_fact(
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
    rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    supersede_relation(
        db, db.get(Campaign, cid), rel.id, epistemic_state="suspected",
        operation_id="op-r2",
    )
    db.commit()
    with pytest.raises(ValueError):
        supersede_relation(
            db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
            operation_id="op-r3",
        )


# ── lookup by canonical entity / source turn / source event ─────────────────

def test_lookup_by_canonical_entity_and_source_refs():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, rel_evt = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, epistemic_state="confirmed",
        visibility="campaign", operation_id="op-rel-src",
    )
    fact, fact_evt = commit_world_write(
        db, cid, rev + 1, create_fact, content="Mara serves the Guild.",
        entity_refs=[mara.id, guild.id], epistemic_state="confirmed",
        visibility="campaign",
        source_event_id=rel_evt.id,
        operation_id="op-fact-src",
    )
    assert fact.source_event_id == rel_evt.id
    assert [str(r.id) for r in list_relations(db, cid, subject_entity_id=mara.id)] == [str(rel.id)]
    # Facts referencing an entity resolve through the join table.
    assert [str(f.id) for f in list_facts(db, cid, entity_id=guild.id)] == [str(fact.id)]
    # Facts/relations record their source turn/attempt (real rows).
    thread = get_or_create_campaign_thread(db, cid, created_by=_owner)
    db.commit()
    src_tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=_owner, raw_content="Source material",
        segments=[{"type": "ic", "text": "Source material."}], thread_id=src_tid,
    )
    db.commit()
    src_turn, src_attempt = coordinate_turn(db, cid, src_tid)
    fact2, _ = commit_world_write(
        db, cid, rev + 2, create_fact, content="Turn-sourced rumor.",
        source_turn_id=src_turn.id, source_attempt_id=src_attempt.id,
        operation_id="op-fact-turn",
    )
    by_turn = list_records_for_source_turn(db, cid, src_turn.id)
    assert [str(f.id) for f in by_turn["facts"]] == [str(fact2.id)]
    # Unknown source event fails closed.
    with pytest.raises(ValueError):
        commit_world_write(
            db, cid, rev + 3, create_fact, content="Bogus provenance.",
            source_event_id=uuid.uuid4(), operation_id="op-fact-bogus",
        )
    db.rollback()


def test_source_turn_attempt_refs_fail_closed():
    from models.dm import DmTurn, DmTurnAttempt

    Fac, cid, owner = _setup()
    db = Fac()
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Material",
        segments=[{"type": "ic", "text": "Material."}], thread_id=tid,
    )
    db.commit()
    turn1, attempt1 = coordinate_turn(db, cid, tid)
    # A second turn/attempt pair in the same campaign (manual rows on a
    # distinct thread to respect the active-turn uniqueness index).
    other_tid = str(uuid.uuid4())
    turn2 = DmTurn(
        id=uuid.uuid4(), campaign_id=cid, thread_id=other_tid, audience="campaign",
        status="pending", source_revision=0, input_set_revision=1, submission_ids=[],
    )
    db.add(turn2)
    db.flush()
    attempt2 = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn2.id, attempt_number=1, campaign_id=cid,
        thread_id=tid, source_revision=0, input_set_revision=1, submission_ids=[],
    )
    db.add(attempt2)
    db.flush()
    # A turn from another campaign (manual rows).
    other_camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Other", revision=0)
    db.add(other_camp)
    db.flush()
    foreign_turn = DmTurn(
        id=uuid.uuid4(), campaign_id=other_camp.id, thread_id=tid,
        audience="campaign", status="pending", source_revision=0,
        input_set_revision=1, submission_ids=[],
    )
    db.add(foreign_turn)
    db.flush()
    # Commit the manual provenance rows: later fail-closed rollbacks in this
    # test must not wipe the fixtures themselves.
    db.commit()
    campaign = db.get(Campaign, cid)

    # Nonexistent turn fails closed.
    with pytest.raises(ValueError):
        create_fact(
            db, campaign, content="Ghost source.",
            source_turn_id=uuid.uuid4(), operation_id="op-ghost-turn",
        )
    db.rollback()
    # Cross-campaign turn fails closed.
    with pytest.raises(ValueError):
        create_fact(
            db, db.get(Campaign, cid), content="Foreign source.",
            source_turn_id=foreign_turn.id, operation_id="op-foreign-turn",
        )
    db.rollback()
    # Mismatched attempt/turn pair fails closed.
    with pytest.raises(ValueError):
        create_fact(
            db, db.get(Campaign, cid), content="Mismatched source.",
            source_turn_id=turn1.id, source_attempt_id=attempt2.id,
            operation_id="op-mismatch",
        )
    db.rollback()
    # Matched pair succeeds.
    fact, created = create_fact(
        db, db.get(Campaign, cid), content="Sourced rumor.",
        source_turn_id=turn1.id, source_attempt_id=attempt1.id,
        operation_id="op-matched",
    )
    assert created is True
    assert fact.source_turn_id == turn1.id
    assert fact.source_attempt_id == attempt1.id
    db.commit()

    # Supersession validates the post-inheritance pair: re-pointing only the
    # turn inherits the old attempt (and vice versa) — both fail closed.
    with pytest.raises(ValueError):
        supersede_fact(
            db, db.get(Campaign, cid), fact.id, source_turn_id=turn2.id,
            operation_id="op-half-turn",
        )
    db.rollback()
    with pytest.raises(ValueError):
        supersede_fact(
            db, db.get(Campaign, cid), fact.id, source_attempt_id=attempt2.id,
            operation_id="op-half-attempt",
        )
    db.rollback()
    new_fact, fcreated = supersede_fact(
        db, db.get(Campaign, cid), fact.id,
        source_turn_id=turn2.id, source_attempt_id=attempt2.id,
        operation_id="op-full-repoint",
    )
    assert fcreated is True
    assert new_fact.source_turn_id == turn2.id
    assert new_fact.source_attempt_id == attempt2.id
    db.commit()

    # Same rule for relations.
    mara, _, rrev = _entities(db, cid, int(db.get(Campaign, cid).revision))
    rel, _ = commit_world_write(
        db, cid, rrev, create_relation,
        subject_entity_id=mara.id, relation_type="knows",
        object_label="someone", source_turn_id=turn1.id,
        source_attempt_id=attempt1.id, operation_id="op-r-src",
    )
    with pytest.raises(ValueError):
        supersede_relation(
            db, db.get(Campaign, cid), rel.id, source_turn_id=turn2.id,
            operation_id="op-r-half-turn",
        )
    db.rollback()
    with pytest.raises(ValueError):
        supersede_relation(
            db, db.get(Campaign, cid), rel.id, source_attempt_id=attempt2.id,
            operation_id="op-r-half-attempt",
        )
    db.rollback()
    new_rel, rcreated = supersede_relation(
        db, db.get(Campaign, cid), rel.id,
        source_turn_id=turn2.id, source_attempt_id=attempt2.id,
        operation_id="op-r-full-repoint",
    )
    assert rcreated is True
    assert new_rel.source_turn_id == turn2.id
    assert new_rel.source_attempt_id == attempt2.id


# ── visibility fail-closed ──────────────────────────────────────────────────

def test_restricted_records_filtered_for_ordinary_member():
    Fac, cid, owner = _setup()
    db = Fac()
    member = uuid.uuid4()
    db.add(Profile(id=member, email="member@example.com"))
    db.add(CampaignMember(campaign_id=cid, user_id=member, role="player"))
    db.commit()
    campaign = db.get(Campaign, cid)
    mara, guild, rev = _entities(db, cid, 0)
    hidden_rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="spies_for",
        object_entity_id=guild.id, visibility="dm_only",
        provenance={"source": "dm_adjudication"}, operation_id="op-hidden-rel",
    )
    # Fail-closed default: unmarked assertions stay restricted.
    default_rel, _ = commit_world_write(
        db, cid, rev + 1, create_relation, subject_entity_id=mara.id, relation_type="owes",
        object_label="a debt", operation_id="op-default-rel",
    )
    assert default_rel.visibility == "dm_only"
    open_fact, _ = commit_world_write(
        db, cid, rev + 2, create_fact, content="The market opens at dawn.",
        visibility="campaign", operation_id="op-open-fact",
    )
    assert hidden_rel.visibility == "dm_only"
    assert open_fact.visibility == "campaign"
    # The AI is the only DM: the owner is a player and receives no dm_only record.
    for viewer in (owner, member):
        assert may_user_receive(db, campaign, "relation", hidden_rel.id, viewer) == {
            "allowed": False, "reason": "dm_only",
        }
        assert may_user_receive(db, campaign, "fact", open_fact.id, viewer)["allowed"] is True


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

    import app.world.facts as facts_mod
    from app.world.facts import find_fact_by_idempotency
    from models.world import WorldFactEntityRef

    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    campaign = db.get(Campaign, cid)
    winner, created = create_fact(
        db, campaign, content="Mara serves the Guild.",
        entity_refs=[mara.id], epistemic_state="confirmed",
        visibility="campaign", idempotency_key="fact-race-1",
    )
    assert created is True
    db.commit()

    # Simulate the race window: the precheck SELECT misses, the upsert
    # absorbs the unique conflict, and the post-insert lookup finds the
    # winner. The loser references a DIFFERENT entity.
    real_find = find_fact_by_idempotency
    calls = {"n": 0}

    def flaky_find(db_, cid_, key_):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(db_, cid_, key_)

    with mock.patch.object(facts_mod, "find_fact_by_idempotency", side_effect=flaky_find):
        loser, created2 = create_fact(
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
    rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    new, created = supersede_relation(
        db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
        operation_id="op-r-sup",
    )
    assert created is True
    db.commit()
    # Exact retry after the prior flipped to superseded: idempotent, no raise.
    same, created2 = supersede_relation(
        db, db.get(Campaign, cid), rel.id, epistemic_state="confirmed",
        operation_id="op-r-sup",
    )
    assert created2 is False
    assert str(same.id) == str(new.id)
    db.commit()

    fact, _ = commit_world_write(
        db, cid, rev + 1, create_fact, content="The vault is sealed.",
        operation_id="op-f1",
    )
    new_fact, fcreated = supersede_fact(
        db, db.get(Campaign, cid), fact.id, epistemic_state="confirmed",
        operation_id="op-f-sup",
    )
    assert fcreated is True
    db.commit()
    same_fact, fcreated2 = supersede_fact(
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
    rel_a, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-ra",
    )
    rel_b, _ = commit_world_write(
        db, cid, rev + 1, create_relation, subject_entity_id=mara.id, relation_type="owes",
        object_label="a debt", operation_id="op-rb",
    )
    supersede_relation(
        db, campaign, rel_a.id, epistemic_state="confirmed",
        operation_id="op-shared-key",
    )
    db.commit()
    # Same key reused against a DIFFERENT prior: fail closed, not mislinked.
    with pytest.raises(ValueError):
        supersede_relation(
            db, db.get(Campaign, cid), rel_b.id, epistemic_state="confirmed",
            operation_id="op-shared-key",
        )
    db.rollback()
    assert get_relation_strict(db, cid, rel_b.id).status == "active"


def test_new_version_status_superseded_is_rejected():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    rel, _ = commit_world_write(
        db, cid, rev, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, operation_id="op-r1",
    )
    with pytest.raises(ValueError):
        supersede_relation(
            db, db.get(Campaign, cid), rel.id, new_status="superseded",
            operation_id="op-r-bad",
        )
    db.rollback()
    fact, _ = commit_world_write(
        db, cid, rev + 1, create_fact, content="The vault is sealed.",
        operation_id="op-f1",
    )
    with pytest.raises(ValueError):
        supersede_fact(
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
        create_relation(
            db, campaign, subject_entity_id=mara.id, relation_type="owes",
            object_label=long_label, operation_id="op-long",
        )
    db.rollback()
    # 256-char boundary is accepted verbatim.
    ok_label = "y" * 256
    rel, created = create_relation(
        db, db.get(Campaign, cid), subject_entity_id=mara.id,
        relation_type="owes", object_label=ok_label, operation_id="op-ok",
    )
    assert created is True
    assert rel.object_label == ok_label
    db.commit()
    with pytest.raises(ValueError):
        supersede_relation(
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


def test_shared_explicit_key_across_effects_rejected():
    from app.dm.contract import ContractValidationError, normalize_contract
    from app.dm.turns import stage_validated_attempt

    def _contract_facts():
        return {
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond", "reason": "shared key",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "Mara nods.", "claim_kind": "observation",
                "origin": "dm_adjudication",
            }]}],
            "staged_effects": [
                {"id": "eff-fact-a", "effect_type": "assert_fact",
                 "arguments": {"content": "First rumor.", "idempotency_key": "shared-key"}},
                {"id": "eff-fact-b", "effect_type": "assert_fact",
                 "arguments": {"content": "Second rumor.", "idempotency_key": "shared-key"}},
            ],
        }

    def _contract_relations(gid):
        return {
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond", "reason": "shared key",
            "beats": [{"id": "beat_1", "type": "narration", "claims": [{
                "text": "Mara nods.", "claim_kind": "observation",
                "origin": "dm_adjudication",
            }]}],
            "staged_effects": [
                {"id": "eff-rel-a", "effect_type": "upsert_relation", "arguments": {
                    "subject_entity_id": gid, "relation_type": "knows",
                    "object_label": "a", "idempotency_key": "shared-rel-key"}},
                {"id": "eff-rel-b", "effect_type": "upsert_relation", "arguments": {
                    "subject_entity_id": gid, "relation_type": "owes",
                    "object_label": "b", "idempotency_key": "shared-rel-key"}},
            ],
        }

    # Distinct IDs but one shared explicit key: rejected for both tables.
    with pytest.raises(ContractValidationError):
        normalize_contract(_contract_facts())
    with pytest.raises(ContractValidationError):
        normalize_contract(_contract_relations(str(uuid.uuid4())))

    # Staging guard mirrors the invariant for raw dicts.
    Fac, cid, owner = _setup()
    db = Fac()
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Shared",
        segments=[{"type": "ic", "text": "Shared."}], thread_id=tid,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, cid, tid)
    with pytest.raises(ValueError, match="idempotency keys must be unique"):
        stage_validated_attempt(db, attempt.id, _contract_facts())
    db.rollback()

    # Distinct explicit keys still validate.
    ok = _contract_facts()
    ok["staged_effects"][1]["arguments"]["idempotency_key"] = "other-key"
    assert len(normalize_contract(ok).staged_effects) == 2


def test_explicit_staged_key_reused_across_turns_stays_distinct():
    Fac, cid, owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)

    def _commit_turn_with(content, relation_type, object_label):
        accept_submission(
            db, campaign_id=cid, user_id=owner, raw_content=content,
            segments=[{"type": "ic", "text": content}], thread_id=tid,
        )
        db.commit()
        turn, attempt = coordinate_turn(db, cid, tid)
        attempt.staged_effects = [
            {"id": "eff-f", "effect_type": "assert_fact", "arguments": {
                "content": content, "epistemic_state": "claimed",
                "visibility": "campaign", "idempotency_key": "rumor",
            }},
            {"id": "eff-r", "effect_type": "upsert_relation", "arguments": {
                "subject_entity_id": str(mara.id), "relation_type": relation_type,
                "object_entity_id": str(guild.id) if object_label is None else None,
                "object_label": object_label, "visibility": "campaign",
                "idempotency_key": "bond",
            }},
        ]
        attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
        db.flush()
        db.commit()
        stream = _stream(db, turn, attempt)
        db.commit()
        mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
        return commit_turn(db, turn.id, attempt.id)

    _t1, _a1, event1 = _commit_turn_with("First rumor.", "knows", None)
    assert event1 is not None
    # A later turn reusing the same explicit keys must store its own records,
    # not collapse onto the older turn's rows as false duplicates.
    _t2, _a2, event2 = _commit_turn_with("Second rumor.", "owes", "a debt")
    assert event2 is not None
    assert str(event2.id) != str(event1.id)
    assert sorted(f.content for f in list_facts(db, cid)) == ["First rumor.", "Second rumor."]
    assert sorted(r.relation_type for r in list_relations(db, cid)) == ["knows", "owes"]


def test_mixed_explicit_and_generated_keys_stay_disjoint():
    Fac, cid, owner = _setup()
    db = Fac()
    mara, guild, _rev = _entities(db, cid, 0)
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="Mixed keys",
        segments=[{"type": "ic", "text": "Mixed keys."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    # fact-b's explicit key equals fact-a's effect ID: without disjoint
    # namespaces both would resolve to one durable key and drop a write.
    attempt.staged_effects = [
        {"id": "fact-a", "effect_type": "assert_fact", "arguments": {
            "content": "First rumor.", "visibility": "campaign",
        }},
        {"id": "fact-b", "effect_type": "assert_fact", "arguments": {
            "content": "Second rumor.", "visibility": "campaign",
            "idempotency_key": "fact-a",
        }},
        {"id": "rel-a", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(mara.id), "relation_type": "knows",
            "object_entity_id": str(guild.id), "visibility": "campaign",
        }},
        {"id": "rel-b", "effect_type": "upsert_relation", "arguments": {
            "subject_entity_id": str(mara.id), "relation_type": "owes",
            "object_label": "a debt", "visibility": "campaign",
            "idempotency_key": "rel-a",
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
    assert sorted(f.content for f in list_facts(db, cid)) == ["First rumor.", "Second rumor."]
    assert sorted(r.relation_type for r in list_relations(db, cid)) == ["knows", "owes"]


def test_restricted_rows_do_not_mask_visible_rows_under_limit():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    # Hidden rows sort before the visible ones (created first).
    cur = rev
    for i in range(3):
        commit_world_write(
            db, cid, cur, create_relation, subject_entity_id=mara.id,
            relation_type=f"hidden_rel_{i}", object_label=f"secret {i}",
            visibility="dm_only", operation_id=f"op-hidden-rel-{i}",
        )
        cur += 1
        commit_world_write(
            db, cid, cur, create_fact, content=f"Secret {i}.",
            visibility="dm_only", operation_id=f"op-hidden-fact-{i}",
        )
        cur += 1
    # One visible row of each kind, last in created_at order.
    rel, _ = commit_world_write(
        db, cid, cur, create_relation, subject_entity_id=mara.id, relation_type="works_for",
        object_entity_id=guild.id, visibility="campaign",
        operation_id="op-open-rel",
    )
    cur += 1
    fact, _ = commit_world_write(
        db, cid, cur, create_fact, content="The market opens at dawn.",
        visibility="campaign", operation_id="op-open-fact",
    )
    # Member-equivalent query: hidden rows must not consume the window —
    # the visible row is returned at both the default limit and limit=1.
    assert [str(r.id) for r in list_relations(db, cid, exclude_restricted=True)] == [str(rel.id)]
    assert [str(r.id) for r in list_relations(db, cid, exclude_restricted=True, limit=1)] == [str(rel.id)]
    assert [str(f.id) for f in list_facts(db, cid, exclude_restricted=True)] == [str(fact.id)]
    assert [str(f.id) for f in list_facts(db, cid, exclude_restricted=True, limit=1)] == [str(fact.id)]
    # Authority view is unchanged (all rows, hidden first).
    assert len(list_relations(db, cid)) == 4
    assert len(list_facts(db, cid)) == 4


def test_widening_supersession_drops_restricted_metadata():
    Fac, cid, _owner = _setup()
    db = Fac()
    mara, guild, rev = _entities(db, cid, 0)
    secrets = {
        "provenance": {"source": "npc_utterance", "dm_private_context": "Mara lies about the retreat."},
        "details": {"dm_note": "Guild is broke."},
        "grants": {"dm_only_flag": True},
    }
    fact, _ = commit_world_write(
        db, cid, rev, create_fact, content="Mara guards the bridge.",
        entity_refs=[mara.id], epistemic_state="false",
        visibility="dm_only", operation_id="op-f-secret",
        **secrets,
    )
    rel, _ = commit_world_write(
        db, cid, rev + 1, create_relation, subject_entity_id=mara.id, relation_type="spies_for",
        object_entity_id=guild.id, visibility="dm_only",
        operation_id="op-r-secret", **secrets,
    )
    # Widen to member-visible without explicit metadata: successor keeps
    # only the record itself, never the prior DM-only context.
    open_fact, _ = commit_world_write(
        db, cid, rev + 2, supersede_fact, fact.id, content="Mara guards the bridge.",
        epistemic_state="believed", visibility="campaign",
        operation_id="op-f-open",
    )
    assert open_fact.provenance == {}
    assert open_fact.details == {}
    assert open_fact.grants == {}
    open_rel, _ = commit_world_write(
        db, cid, rev + 3, supersede_relation, rel.id, epistemic_state="believed",
        visibility="campaign", operation_id="op-r-open",
    )
    assert open_rel.provenance == {}
    assert open_rel.details == {}
    # Prior history still preserves the secrets for the DM.
    assert get_relation_strict(db, cid, rel.id).provenance["dm_private_context"].startswith("Mara lies")
    # Explicitly supplied metadata on a widening supersession is kept.
    open_fact2, _ = commit_world_write(
        db, cid, rev + 4, supersede_fact, open_fact.id, epistemic_state="confirmed",
        provenance={"source": "dm_adjudication", "note": "party witnessed it"},
        operation_id="op-f-open2",
    )
    assert open_fact2.provenance == {"source": "dm_adjudication", "note": "party witnessed it"}
    # Non-widening supersession still inherits.
    still_open, _ = commit_world_write(
        db, cid, rev + 5, supersede_fact, open_fact2.id,
        epistemic_state="confirmed", operation_id="op-f-same",
    )
    assert still_open.provenance == {"source": "dm_adjudication", "note": "party witnessed it"}


