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
from app.campaigns.events import RevisionConflictError, commit_campaign_mutation  # noqa: E402
from app.world.npcs import (  # noqa: E402
    apply_npc_state_inline,
    build_npc_decision_context,
    get_npc_state,
    project_npc_state,
    require_legal_npc_choice,
    update_npc_state_authoritative,
)
from app.world.service import create_entity_authoritative  # noqa: E402
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
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    assert get_npc_state(db, cid, npc.id) is None
    baseline_id = npc.id

    state, event = update_npc_state_authoritative(
        db, cid, npc.id, 1, role="harbor guide", location_name="South Pier",
        operation_id="baseline", **_source(created),
    )
    assert state.role == "harbor guide"
    assert state.goals == []
    assert event.event_type == "world.npc_state_updated"

    enriched, _ = update_npc_state_authoritative(
        db, cid, npc.id, 2, goals=[{"id": "protect_docks", "summary": "Protect the docks"}],
        disposition={"party": "wary"}, resources=[{"id": "skiff", "kind": "vehicle"}],
        current_activity="watching the tide", importance="major", depth=3,
        operation_id="enrich", **_source(event),
    )
    assert enriched.entity_id == baseline_id
    assert db.get(WorldEntity, baseline_id).name == "Mara"
    assert enriched.state_revision == 2
    assert enriched.importance == "major"
    assert enriched.provenance["source"] == "committed_gameplay"


def test_hidden_motives_are_not_in_member_projection_but_owner_can_request_them():
    db, cid, owner, member = _setup()
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    update_npc_state_authoritative(
        db, cid, npc.id, 1, role="guide", goals=[{"summary": "Betray the party"}],
        current_activity="leading the party", operation_id="state", **_source(created),
    )
    campaign = db.get(Campaign, cid)
    requested = ["role", "goals", "current_activity"]
    player_view = project_npc_state(db, campaign, npc.id, member, fields=requested)
    assert player_view["fields"] == {"role": "guide"}
    owner_view = project_npc_state(db, campaign, npc.id, owner, fields=requested)
    assert owner_view["fields"]["goals"][0]["summary"] == "Betray the party"
    assert owner_view["fields"]["current_activity"] == "leading the party"


def test_location_activity_update_requires_fresh_campaign_revision():
    db, cid, _, _ = _setup()
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    location, _ = create_entity_authoritative(db, cid, 1, entity_type="location", name="Docks", operation_id="loc")
    update_npc_state_authoritative(
        db, cid, npc.id, 2, location_entity_id=location.id, current_activity="working",
        operation_id="move", **_source(created),
    )
    with pytest.raises(RevisionConflictError):
        update_npc_state_authoritative(
            db, cid, npc.id, 2, current_activity="stale overwrite",
            operation_id="stale", **_source(created),
        )
    state = get_npc_state(db, cid, npc.id)
    assert state.current_activity == "working"
    assert state.location_entity_id == location.id


def test_progressive_depth_and_importance_cannot_be_downgraded():
    db, cid, _, _ = _setup()
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    _, enriched = update_npc_state_authoritative(
        db, cid, npc.id, 1, importance="major", depth=4, operation_id="deep", **_source(created),
    )
    with pytest.raises(ValueError, match="importance cannot decrease"):
        update_npc_state_authoritative(
            db, cid, npc.id, 2, importance="supporting", operation_id="shallow", **_source(enriched),
        )
    assert get_npc_state(db, cid, npc.id).importance == "major"


def test_bounded_decision_context_accepts_only_domain_supplied_candidates_and_never_mutates():
    db, cid, owner, _ = _setup()
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    state, _ = update_npc_state_authoritative(
        db, cid, npc.id, 1, resources=[{"id": "dagger"}], depth=2,
        operation_id="state", **_source(created),
    )
    campaign = db.get(Campaign, cid)
    context = build_npc_decision_context(
        db, campaign, npc.id, owner, fields=["resources"],
        candidates=[
            {"id": "wait", "label": "Wait"},
            {"id": "attack:dagger", "label": "Attack with dagger"},
        ],
    )
    assert context["candidate_count"] == 2
    assert require_legal_npc_choice(context, "attack:dagger")["label"] == "Attack with dagger"
    with pytest.raises(ValueError, match="supplied legal candidate"):
        require_legal_npc_choice(context, "cast:meteor_swarm")
    assert get_npc_state(db, cid, npc.id).state_revision == state.state_revision


def test_partial_enrichment_validation_failure_does_not_corrupt_identity():
    db, cid, _, _ = _setup()
    npc, created = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    with pytest.raises(ValueError, match="goals must be a list"):
        update_npc_state_authoritative(
            db, cid, npc.id, 1, goals={"bad": "shape"}, operation_id="bad", **_source(created),
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


def test_succeeded_turn_attempt_provenance_passes_standalone_gate():
    db, cid, _, _ = _setup()
    npc, _ = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    turn, attempt = _succeeded_turn_pair(db, cid)
    rev = db.get(Campaign, cid).revision
    state, _ = update_npc_state_authoritative(
        db, cid, npc.id, rev, role="harbor guide", operation_id="turn-src",
        provenance={"source": "dm_turn"},
        source_turn_id=turn.id, source_attempt_id=attempt.id,
    )
    assert state.source_turn_id == turn.id
    assert state.source_attempt_id == attempt.id

    # A mid-flight (streaming) turn cannot back a standalone write.
    hot_turn, hot_attempt = _succeeded_turn_pair(
        db, cid, turn_status="streaming", attempt_status="streaming",
    )
    rev = db.get(Campaign, cid).revision
    with pytest.raises(ValueError, match="committed gameplay"):
        update_npc_state_authoritative(
            db, cid, npc.id, rev, role="stowaway", operation_id="hot-src",
            provenance={"source": "dm_turn"},
            source_turn_id=hot_turn.id, source_attempt_id=hot_attempt.id,
        )

    # A mismatched turn/attempt pair fails closed even when the turn succeeded.
    other_turn, _ = _succeeded_turn_pair(db, cid)
    with pytest.raises(ValueError, match="does not belong to source_turn"):
        update_npc_state_authoritative(
            db, cid, npc.id, rev, role="stowaway", operation_id="mixed-src",
            provenance={"source": "dm_turn"},
            source_turn_id=other_turn.id, source_attempt_id=attempt.id,
        )
    assert get_npc_state(db, cid, npc.id).role == "harbor guide"


def test_inline_npc_write_participates_in_outer_transaction_and_rolls_back():
    db, cid, _, _ = _setup()
    npc, _ = create_entity_authoritative(db, cid, 0, entity_type="npc", name="Mara", operation_id="npc")
    # Mid-commit rows (still streaming): the inline writer records them as
    # provenance inside the outer transaction instead of gating on status.
    turn, attempt = _succeeded_turn_pair(
        db, cid, turn_status="streaming", attempt_status="streaming",
    )
    campaign = db.get(Campaign, cid)
    prior = int(campaign.revision)

    def _mutate(c):
        apply_npc_state_inline(
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
        apply_npc_state_inline(
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
