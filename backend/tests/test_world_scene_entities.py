"""Issue #209 — authoritative current scene, fictional time, canonical entities."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

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
from app.campaigns.events import RevisionConflictError, commit_campaign_mutation, list_campaign_events  # noqa: E402
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from app.world.service import (  # noqa: E402
    build_current_scene_context_record,
    create_entity_authoritative,
    create_entity_inline,
    get_current_scene,
    get_current_scene_dict,
    list_entities,
    set_scene_authoritative,
)
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.dm import DMStream, DMStreamChunk  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import CampaignCurrentScene, WorldEntity  # noqa: E402


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
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="World campaign", revision=0)
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=owner, role="owner"))
    db.commit()
    db.refresh(camp)
    return Fac, camp.id, owner


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


# ── canonical creation ──────────────────────────────────────────────────────

def test_canonical_npc_and_location_creation_have_stable_ids():
    Fac, cid, _owner = _setup()
    db = Fac()
    npc, evt1 = create_entity_authoritative(
        db, cid, 0, entity_type="npc", name="Mira the Guide",
        summary="A hooded guide.", operation_id="op-npc-1",
    )
    assert evt1.sequence == 1
    loc, evt2 = create_entity_authoritative(
        db, cid, 1, entity_type="location", name="Ember Gate",
        operation_id="op-loc-1",
    )
    assert evt2.sequence == 2
    assert npc.id != loc.id
    # Stable IDs reusable across reads
    assert db.get(WorldEntity, npc.id).name == "Mira the Guide"
    assert db.get(WorldEntity, loc.id).entity_type == "location"
    assert db.get(Campaign, cid).revision == 2


def test_entity_types_cover_faction_object_landmark_and_custom():
    Fac, cid, _owner = _setup()
    db = Fac()
    rev = 0
    for etype in ("faction", "organization", "object", "item", "landmark", "custom_beast"):
        ent, _evt = create_entity_authoritative(
            db, cid, rev, entity_type=etype, name=f"Test {etype}",
            operation_id=f"op-{etype}",
        )
        assert ent.entity_type == etype
        rev += 1
    assert len(list_entities(db, cid)) == 6
    assert len(list_entities(db, cid, entity_type="faction")) == 1


def test_idempotent_jit_creation_duplicate_retry_creates_nothing():
    Fac, cid, _owner = _setup()
    db = Fac()
    first, evt = create_entity_authoritative(
        db, cid, 0, entity_type="npc", name="Mira",
        operation_id="op-dup-1", idempotency_key="jit:attempt-1:tmp_npc_1",
    )
    assert evt is not None
    # Duplicate retry with same key at the new revision returns existing, no event
    second, evt2 = create_entity_authoritative(
        db, cid, 1, entity_type="npc", name="Mira (retry)",
        operation_id="op-dup-1", idempotency_key="jit:attempt-1:tmp_npc_1",
    )
    assert str(second.id) == str(first.id)
    assert evt2 is None
    assert second.name == "Mira"  # original preserved
    assert len(list_entities(db, cid)) == 1
    assert db.get(Campaign, cid).revision == 1  # no extra revision bump


# ── scene / fictional time ──────────────────────────────────────────────────

def test_scene_location_transition_and_fictional_time_update():
    Fac, cid, _owner = _setup()
    db = Fac()
    loc, _ = create_entity_authoritative(
        db, cid, 0, entity_type="location", name="Ember Gate", operation_id="op-loc",
    )
    scene, evt = set_scene_authoritative(
        db, cid, 1, location_entity_id=loc.id, location_name="Ember Gate",
        fictional_time="Day 3, dusk",
        present_actors=[{"entity_id": "pc-1", "name": "Aria"}],
        environment={"weather": "ashfall"}, operation_id="op-scene-1",
    )
    assert evt.sequence == 2
    assert scene.location_name == "Ember Gate"
    assert scene.fictional_time == "Day 3, dusk"
    assert scene.present_actors[0]["name"] == "Aria"

    scene2, evt2 = set_scene_authoritative(
        db, cid, 2, location_name="Sunken Archive", fictional_time="Day 4, dawn",
        present_actors=["Aria", "Bram"], operation_id="op-scene-2",
    )
    assert evt2.sequence == 3
    assert scene2.location_name == "Sunken Archive"
    assert scene2.fictional_time == "Day 4, dawn"
    # Transient change did not delete durable entity history
    assert db.get(WorldEntity, loc.id) is not None
    assert db.get(Campaign, cid).revision == 3


def test_stale_revision_scene_update_fails_safely():
    Fac, cid, _owner = _setup()
    db = Fac()
    set_scene_authoritative(db, cid, 0, location_name="A", operation_id="op-a")
    with pytest.raises(RevisionConflictError):
        set_scene_authoritative(db, cid, 0, location_name="STALE", operation_id="op-stale")
    scene = get_current_scene(db, cid)
    assert scene.location_name == "A"
    assert db.get(Campaign, cid).revision == 1
    assert len(list_campaign_events(db, cid)) == 1


def test_current_scene_retrieval_without_parsing_history():
    Fac, cid, _owner = _setup()
    db = Fac()
    assert get_current_scene_dict(db, cid) is None
    set_scene_authoritative(
        db, cid, 0, location_name="Ember Gate", fictional_time="Day 3",
        present_actors=[{"name": "Aria"}, {"name": "Bram"}],
        environment={"light": "torchlight"}, operation_id="op-s",
    )
    current = get_current_scene_dict(db, cid)
    assert current["location_name"] == "Ember Gate"
    assert current["fictional_time"] == "Day 3"
    assert [a["name"] for a in current["present_actors"]] == ["Aria", "Bram"]
    assert current["environment"] == {"light": "torchlight"}

    campaign = db.get(Campaign, cid)
    value = build_current_scene_context_record(db, campaign)
    assert value["location_name"] == "Ember Gate"
    assert value["present_actor_names"] == ["Aria", "Bram"]


def test_transient_scene_change_preserves_entity_history_and_visibility_hook():
    Fac, cid, _owner = _setup()
    db = Fac()
    npc, _ = create_entity_authoritative(
        db, cid, 0, entity_type="npc", name="Secret Informant",
        visibility="dm_only", operation_id="op-secret",
    )
    assert npc.visibility == "dm_only"
    set_scene_authoritative(
        db, cid, 1, location_name="Tavern", visibility="dm_only", operation_id="op-h",
    )
    set_scene_authoritative(db, cid, 2, location_name="Docks", operation_id="op-d")
    assert db.get(WorldEntity, npc.id).visibility == "dm_only"
    assert get_current_scene(db, cid).location_name == "Docks"
    assert get_current_scene(db, cid).visibility == "dm_only"


# ── transactional JIT promotion from a committed turn ───────────────────────

def test_jit_promotion_from_committed_turn_exactly_once():
    Fac, cid, owner = _setup()
    db = Fac()
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="We enter",
        segments=[{"type": "ic", "text": "We enter."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    # Commit a structured contract snapshot with one new-entity proposal
    attempt.contract_snapshot = {
        "contract_version": "dm_turn_contract_v1",
        "new_entities": [{
            "temp_id": "tmp_npc_1", "kind": "npc",
            "public_name": "Mira the Guide", "role": "guide",
            "public_summary": "A hooded guide.",
        }],
        "staged_effects": [],
    }
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    _t, _a, event = commit_turn(db, turn.id, attempt.id)
    assert event is not None
    entities = list_entities(db, cid, entity_type="npc")
    assert len(entities) == 1
    assert entities[0].name == "Mira the Guide"
    assert entities[0].source_attempt_id == attempt.id
    # Duplicate commit replay (same operation) promotes nothing new
    _t2, _a2, event2 = commit_turn(db, turn.id, attempt.id)
    assert len(list_entities(db, cid, entity_type="npc")) == 1
    assert str(event2.id) == str(event.id)


def test_failed_entity_commit_leaves_no_half_created_authority():
    Fac, cid, _owner = _setup()
    db = Fac()

    def bad_mutate(campaign):
        create_entity_inline(
            db, campaign, entity_type="npc", name="Half-Made",
            idempotency_key="half-1",
        )
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        commit_campaign_mutation(
            db, cid, 0, event_type="world.entity_created", mutate=bad_mutate,
        )
    db.rollback()
    assert list_entities(db, cid) == []
    assert db.get(Campaign, cid).revision == 0
    assert list_campaign_events(db, cid) == []


def test_scene_update_effect_commits_transactionally_with_turn():
    Fac, cid, owner = _setup()
    db = Fac()
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="We march on",
        segments=[{"type": "ic", "text": "We march on."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.staged_effects = [{
        "id": "eff-scene-1", "effect_type": "update_scene",
        "arguments": {
            "scene_patch": {
                "location_name": "Ember Gate", "fictional_time": "Day 3, dusk",
                "present_actors": [{"name": "Aria"}],
            },
            "reason": "party arrived",
        },
    }]
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    _t, _a, event = commit_turn(db, turn.id, attempt.id)
    assert event.sequence == 1
    scene = get_current_scene(db, cid)
    assert scene.location_name == "Ember Gate"
    assert scene.fictional_time == "Day 3, dusk"
    assert scene.source_attempt_id == attempt.id
