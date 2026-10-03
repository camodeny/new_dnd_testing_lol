"""Issue #215 — progressive NPC state, visibility, and bounded choices."""

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
from tests.support.world_writes import commit_world_write  # noqa: E402
from app.campaigns.events import RevisionConflictError, commit_campaign_mutation  # noqa: E402
from app.world.npcs import (  # noqa: E402
    get_npc_state,
    apply_npc_state,
)
from app.world.service import create_entity  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


def _setup():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    factory = sessionmaker(bind=eng, expire_on_commit=False)
    db = factory()
    owner, member = uuid.uuid4(), uuid.uuid4()
    db.add_all([Profile(id=owner, email="owner@example.com"), Profile(id=member, email="member@example.com")])
    campaign = Campaign(id=uuid.uuid4(), owner_id=owner, name="NPC campaign", revision=0)
    db.add(campaign)
    db.flush()
    db.add_all([
        CampaignMember(campaign_id=campaign.id, user_id=owner, role="owner"),
        CampaignMember(campaign_id=campaign.id, user_id=member, role="player"),
    ])
    db.commit()
    return db, campaign.id, owner, member


def _source(event):
    return {"provenance": {"source": "committed_gameplay"}, "source_event_id": event.id}


def test_lightweight_npc_needs_no_dossier_then_enriches_without_identity_change():
    db, cid, owner, _ = _setup()
    npc, created = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    assert get_npc_state(db, cid, npc.id) is None
    baseline_id = npc.id

    state, event = commit_world_write(
        db, cid, 1, apply_npc_state, npc.id, role="harbor guide", location_name="South Pier",
        operation_id="baseline", **_source(created),
    )
    assert state.role == "harbor guide"
    assert state.goals == []

    enriched, _ = commit_world_write(
        db, cid, 2, apply_npc_state, npc.id, goals=[{"id": "protect_docks", "summary": "Protect the docks"}],
        disposition={"party": "wary"}, resources=[{"id": "skiff", "kind": "vehicle"}],
        current_activity="watching the tide", importance="major", depth=3,
        operation_id="enrich", **_source(event),
    )
    assert enriched.entity_id == baseline_id
    assert db.get(WorldEntity, baseline_id).name == "Mara"
    assert enriched.state_revision == 2
    assert enriched.importance == "major"
    assert enriched.provenance["source"] == "committed_gameplay"


def test_location_activity_update_requires_fresh_campaign_revision():
    db, cid, _, _ = _setup()
    npc, created = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    location, _ = commit_world_write(db, cid, 1, create_entity, entity_type="location", name="Docks", operation_id="loc")
    commit_world_write(
        db, cid, 2, apply_npc_state, npc.id, location_entity_id=location.id, current_activity="working",
        operation_id="move", **_source(created),
    )
    with pytest.raises(RevisionConflictError):
        commit_world_write(
            db, cid, 2, apply_npc_state, npc.id, current_activity="stale overwrite",
            operation_id="stale", **_source(created),
        )
    state = get_npc_state(db, cid, npc.id)
    assert state.current_activity == "working"
    assert state.location_entity_id == location.id


def test_progressive_depth_and_importance_cannot_be_downgraded():
    db, cid, _, _ = _setup()
    npc, created = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    _, enriched = commit_world_write(
        db, cid, 1, apply_npc_state, npc.id, importance="major", depth=4, operation_id="deep", **_source(created),
    )
    with pytest.raises(ValueError, match="importance cannot decrease"):
        commit_world_write(
            db, cid, 2, apply_npc_state, npc.id, importance="supporting", operation_id="shallow", **_source(enriched),
        )
    assert get_npc_state(db, cid, npc.id).importance == "major"


def test_partial_enrichment_validation_failure_does_not_corrupt_identity():
    db, cid, _, _ = _setup()
    npc, created = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    with pytest.raises(ValueError, match="goals must be a list"):
        commit_world_write(
            db, cid, 1, apply_npc_state, npc.id, goals={"bad": "shape"}, operation_id="bad", **_source(created),
        )
    assert db.get(WorldEntity, npc.id).name == "Mara"
    assert get_npc_state(db, cid, npc.id) is None


def _succeeded_turn_pair(db, cid, *, turn_status="succeeded", attempt_status="succeeded"):
    rev = db.get(Campaign, cid).revision
    turn = DmTurn(
        id=uuid.uuid4(), campaign_id=cid, thread_id="main", status=turn_status,
        source_revision=rev, input_set_revision=1, submission_ids=[],
    )
    db.add(turn)
    db.flush()
    attempt = DmTurnAttempt(
        id=uuid.uuid4(), turn_id=turn.id, attempt_number=1, status=attempt_status,
        campaign_id=cid, thread_id="main", source_revision=rev,
        input_set_revision=1, submission_ids=[],
    )
    db.add(attempt)
    db.flush()
    return turn, attempt


def test_turn_attempt_provenance_recorded_and_mismatch_fails_closed():
    db, cid, _, _ = _setup()
    npc, _ = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    turn, attempt = _succeeded_turn_pair(db, cid)
    rev = db.get(Campaign, cid).revision
    state, _ = commit_world_write(
        db, cid, rev, apply_npc_state, npc.id, role="harbor guide", operation_id="turn-src",
        provenance={"source": "dm_turn"},
        source_turn_id=turn.id, source_attempt_id=attempt.id,
    )
    assert state.source_turn_id == turn.id
    assert state.source_attempt_id == attempt.id

    rev = db.get(Campaign, cid).revision
    # A mismatched turn/attempt pair fails closed even when the turn succeeded.
    other_turn, _ = _succeeded_turn_pair(db, cid)
    with pytest.raises(ValueError, match="does not belong to source_turn"):
        commit_world_write(
            db, cid, rev, apply_npc_state, npc.id, role="stowaway", operation_id="mixed-src",
            provenance={"source": "dm_turn"},
            source_turn_id=other_turn.id, source_attempt_id=attempt.id,
        )
    assert get_npc_state(db, cid, npc.id).role == "harbor guide"


def test_inline_npc_write_participates_in_outer_transaction_and_rolls_back():
    db, cid, _, _ = _setup()
    npc, _ = commit_world_write(db, cid, 0, create_entity, entity_type="npc", name="Mara", operation_id="npc")
    # Mid-commit rows (still streaming): the inline writer records them as
    # provenance inside the outer transaction instead of gating on status.
    turn, attempt = _succeeded_turn_pair(
        db, cid, turn_status="streaming", attempt_status="streaming",
    )
    campaign = db.get(Campaign, cid)
    prior = int(campaign.revision)

    def _mutate(c):
        apply_npc_state(
            db, c, npc.id, new_revision=prior + 1, role="harbor guide",
            provenance={"source": "post_turn"}, source_turn_id=turn.id,
            source_attempt_id=attempt.id, operation_id="inline",
        )

    _, outer = commit_campaign_mutation(
        db, cid, prior, event_type="dm.turn_resolved",
        operation_id="outer", mutate=_mutate,
    )
    row = get_npc_state(db, cid, npc.id)
    assert row is not None and row.role == "harbor guide"
    assert row.source_turn_id == turn.id and row.source_attempt_id == attempt.id
    assert row.campaign_revision == prior + 1 == outer.sequence

    # A post-write failure inside the outer transaction rolls the NPC write
    # back with it: no half-applied dossier, no revision advance.
    def _boom(c):
        apply_npc_state(
            db, c, npc.id, new_revision=prior + 2, role="stowaway",
            provenance={"source": "post_turn"}, source_turn_id=turn.id,
            source_attempt_id=attempt.id, operation_id="boom",
        )
        raise RuntimeError("post-turn consolidation failed")

    with pytest.raises(RuntimeError, match="consolidation failed"):
        commit_campaign_mutation(
            db, cid, prior + 1, event_type="dm.turn_resolved",
            operation_id="boom", mutate=_boom,
        )
    rolled_back = get_npc_state(db, cid, npc.id)
    assert rolled_back.role == "harbor guide"
    assert rolled_back.state_revision == row.state_revision
    assert db.get(Campaign, cid).revision == prior + 1
