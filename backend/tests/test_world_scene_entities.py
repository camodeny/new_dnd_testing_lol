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
from tests.support.world_writes import commit_world_write  # noqa: E402
from app.campaigns.events import RevisionConflictError, commit_campaign_mutation, list_campaign_events  # noqa: E402
from app.dm.turns import commit_turn, coordinate_turn, mark_streaming_started  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from app.world.service import (  # noqa: E402
    build_current_scene_context_record,
    create_entity,
    get_current_scene,
    list_entities,
    apply_scene_update,
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
    npc, evt1 = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Mira the Guide",
        summary="A hooded guide.", operation_id="op-npc-1",
    )
    assert evt1.sequence == 1
    loc, evt2 = commit_world_write(
        db, cid, 1, create_entity, entity_type="location", name="Ember Gate",
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
        ent, _evt = commit_world_write(
            db, cid, rev, create_entity, entity_type=etype, name=f"Test {etype}",
            operation_id=f"op-{etype}",
        )
        assert ent.entity_type == etype
        rev += 1
    assert len(list_entities(db, cid)) == 6
    assert len(list_entities(db, cid, entity_type="faction")) == 1


def test_idempotent_jit_creation_duplicate_retry_creates_nothing():
    Fac, cid, _owner = _setup()
    db = Fac()
    campaign = db.get(Campaign, cid)
    first, created = create_entity(
        db, campaign, entity_type="npc", name="Mira",
        operation_id="op-dup-1", idempotency_key="jit:attempt-1:tmp_npc_1",
    )
    assert created is True
    # Duplicate retry with the same key returns the existing row.
    second, created_again = create_entity(
        db, campaign, entity_type="npc", name="Mira (retry)",
        operation_id="op-dup-1", idempotency_key="jit:attempt-1:tmp_npc_1",
    )
    assert str(second.id) == str(first.id)
    assert created_again is False
    assert second.name == "Mira"  # original preserved
    assert len(list_entities(db, cid)) == 1


# ── scene / fictional time ──────────────────────────────────────────────────

def test_scene_location_transition_and_fictional_time_update():
    Fac, cid, _owner = _setup()
    db = Fac()
    loc, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="location", name="Ember Gate", operation_id="op-loc",
    )
    scene, evt = commit_world_write(
        db, cid, 1, apply_scene_update, location_entity_id=loc.id, location_name="Ember Gate",
        fictional_time="Day 3, dusk",
        present_actors=[{"entity_id": "pc-1", "name": "Aria"}],
        environment={"weather": "ashfall"}, operation_id="op-scene-1",
    )
    assert evt.sequence == 2
    assert scene.location_name == "Ember Gate"
    assert scene.fictional_time == "Day 3, dusk"
    assert scene.present_actors[0]["name"] == "Aria"

    scene2, evt2 = commit_world_write(
        db, cid, 2, apply_scene_update, location_name="Sunken Archive", fictional_time="Day 4, dawn",
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
    commit_world_write(db, cid, 0, apply_scene_update, location_name="A", operation_id="op-a")
    with pytest.raises(RevisionConflictError):
        commit_world_write(db, cid, 0, apply_scene_update, location_name="STALE", operation_id="op-stale")
    scene = get_current_scene(db, cid)
    assert scene.location_name == "A"
    assert db.get(Campaign, cid).revision == 1
    assert len(list_campaign_events(db, cid)) == 1


def test_current_scene_retrieval_without_parsing_history():
    Fac, cid, _owner = _setup()
    db = Fac()
    assert get_current_scene(db, cid) is None
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Ember Gate", fictional_time="Day 3",
        present_actors=[{"name": "Aria"}, {"name": "Bram"}],
        environment={"light": "torchlight"}, operation_id="op-s",
    )
    current = get_current_scene(db, cid).to_dict()
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
    npc, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="npc", name="Secret Informant",
        visibility="dm_only", operation_id="op-secret",
    )
    assert npc.visibility == "dm_only"
    commit_world_write(
        db, cid, 1, apply_scene_update, location_name="Tavern", visibility="dm_only", operation_id="op-h",
    )
    commit_world_write(db, cid, 2, apply_scene_update, location_name="Docks", operation_id="op-d")
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
        create_entity(
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


def _commit_scene_patch_turn(db, cid, owner, tid, patch):
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="The scene shifts",
        segments=[{"type": "ic", "text": "The scene shifts."}], thread_id=tid,
    )
    db.commit()
    turn, attempt = coordinate_turn(db, cid, tid)
    attempt.staged_effects = [{
        "id": f"eff-clear-{turn.id}", "effect_type": "update_scene",
        "arguments": {"scene_patch": patch, "reason": "clear test"},
    }]
    attempt.contract_snapshot = {"contract_version": "dm_turn_contract_v1", "new_entities": [], "staged_effects": []}
    db.flush()
    db.commit()
    stream = _stream(db, turn, attempt)
    db.commit()
    mark_streaming_started(db, turn.id, attempt.id, stream_id=stream.id)
    return commit_turn(db, turn.id, attempt.id)


def test_staged_update_scene_empty_values_clear_state():
    Fac, cid, owner = _setup()
    db = Fac()
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Ember Gate",
        present_actors=[{"name": "Aria"}, {"name": "Bram"}],
        environment={"weather": "ashfall"}, operation_id="op-seed",
    )
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    _t, _a, event = _commit_scene_patch_turn(
        db, cid, owner, tid, {"present_actors": [], "environment": {}},
    )
    assert event is not None
    scene = get_current_scene(db, cid)
    assert scene.present_actors == []
    assert scene.environment == {}
    # Location untouched by the patch is preserved
    assert scene.location_name == "Ember Gate"


def test_staged_update_scene_empty_alias_values_clear_state():
    Fac, cid, owner = _setup()
    db = Fac()
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Ember Gate",
        present_actors=[{"name": "Aria"}],
        environment={"light": "torchlight"}, operation_id="op-seed-alias",
    )
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    _t, _a, event = _commit_scene_patch_turn(
        db, cid, owner, tid, {"present_actor_names": [], "state": {}},
    )
    assert event is not None
    scene = get_current_scene(db, cid)
    assert scene.present_actors == []
    assert scene.environment == {}


def test_create_entity_inline_idempotency_race_returns_winner():
    from unittest import mock

    import app.world.service as world_service
    from app.world.service import find_entity_by_idempotency

    Fac, cid, _owner = _setup()
    db = Fac()
    campaign = db.get(Campaign, cid)
    winner, created = create_entity(
        db, campaign, entity_type="npc", name="Winner",
        idempotency_key="race-1",
    )
    assert created is True
    db.commit()

    # Simulate the race window: the precheck SELECT misses (concurrent winner
    # not yet visible), then the upsert absorbs the unique conflict and the
    # post-insert lookup finds the winner.
    real_find = find_entity_by_idempotency
    calls = {"n": 0}

    def flaky_find(db_, cid_, key_):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_find(db_, cid_, key_)

    with mock.patch.object(world_service, "find_entity_by_idempotency", side_effect=flaky_find):
        campaign2 = db.get(Campaign, cid)
        loser, created2 = create_entity(
            db, campaign2, entity_type="npc", name="Loser",
            idempotency_key="race-1",
        )
    assert created2 is False
    assert str(loser.id) == str(winner.id)
    assert loser.name == "Winner"
    # Outer transaction was NOT left rollback-only: session still usable.
    db.commit()
    assert len(list_entities(db, cid)) == 1


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


# ── re-review HOLD findings, round 2 (PR #348) ──────────────────────────────

def test_location_reference_explicit_null_clears():
    from app.world.service import UNSET

    Fac, cid, _owner = _setup()
    db = Fac()
    loc, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="location", name="Ember Gate", operation_id="op-loc",
    )
    commit_world_write(
        db, cid, 1, apply_scene_update, location_entity_id=loc.id, location_name="Ember Gate",
        operation_id="op-s1",
    )
    assert get_current_scene(db, cid).location_entity_id == loc.id
    # Explicit null clears the canonical reference (not omission).
    scene, _ = commit_world_write(
        db, cid, 2, apply_scene_update, location_entity_id=None, operation_id="op-clear",
    )
    assert scene.location_entity_id is None
    # Omission is a distinct state from explicit null.
    assert UNSET is not None


def test_location_reference_omission_preserves_despite_name_change():
    Fac, cid, _owner = _setup()
    db = Fac()
    loc, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="location", name="Ember Gate", operation_id="op-loc",
    )
    commit_world_write(
        db, cid, 1, apply_scene_update, location_entity_id=loc.id, location_name="Ember Gate",
        operation_id="op-s1",
    )
    # location_name changes WITHOUT the location_entity_id key: the canonical
    # reference must be preserved, not silently retained-or-cleared.
    scene, _ = commit_world_write(
        db, cid, 2, apply_scene_update, location_name="Ember Gate Annex", operation_id="op-s2",
    )
    assert scene.location_entity_id == loc.id
    assert scene.location_name == "Ember Gate Annex"


def test_staged_patch_explicit_null_location_clears_reference():
    Fac, cid, owner = _setup()
    db = Fac()
    loc, _ = commit_world_write(
        db, cid, 0, create_entity, entity_type="location", name="Ember Gate", operation_id="op-loc",
    )
    commit_world_write(
        db, cid, 1, apply_scene_update, location_entity_id=loc.id, location_name="Ember Gate",
        operation_id="op-s1",
    )
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    _t, _a, event = _commit_scene_patch_turn(
        db, cid, owner, tid,
        {"location_entity_id": None, "location_name": "Open Road"},
    )
    assert event is not None
    scene = get_current_scene(db, cid)
    assert scene.location_entity_id is None
    assert scene.location_name == "Open Road"
    # Durable entity history is untouched by clearing the transient reference.
    assert db.get(WorldEntity, loc.id) is not None


def test_restricted_scene_hidden_from_ordinary_member():
    from app.visibility.policy import visible_to_viewer

    Fac, cid, _owner = _setup()
    db = Fac()
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Secret Lair", visibility="dm_only",
        operation_id="op-s",
    )
    scene = get_current_scene(db, cid)
    assert visible_to_viewer(scene.visibility, True) is True
    assert visible_to_viewer(scene.visibility, False) is False


def test_dm_only_scene_excluded_from_player_narration_projection():
    from app.dm.context import LaneName, assemble_attempt_context

    Fac, cid, owner = _setup()
    db = Fac()
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Secret Lair", visibility="dm_only",
        operation_id="op-s",
    )
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="We sneak in",
        segments=[{"type": "ic", "text": "We sneak in."}], thread_id=tid,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, cid, tid)
    # Empty-but-required non-scene lanes are explicitly not applicable here.
    packet = assemble_attempt_context(
        db, attempt.id,
        supplemental_status={
            LaneName.KNOWLEDGE_VISIBILITY: "not_applicable",
            LaneName.CONTENT_BOUNDARIES: "not_applicable",
            LaneName.DIFFICULTY: "not_applicable",
        },
    )
    lane = next(
        record for record in packet.lanes if record.name == LaneName.CURRENT_SCENE
    )
    assert len(lane.records) == 1
    assert lane.records[0].visibility == "dm_only"
    assert lane.records[0].use == "adjudication_only"
    proj = packet.narration_projection()
    proj_lane = next(
        record for record in proj["lanes"] if record["name"] == "current_scene"
    )
    assert proj_lane["records"] == []


def test_private_scene_context_assembly_stays_hidden():
    from app.dm.context import LaneName, assemble_attempt_context

    Fac, cid, owner = _setup()
    db = Fac()
    commit_world_write(
        db, cid, 0, apply_scene_update, location_name="Hidden Sanctum", visibility="private",
        operation_id="op-s",
    )
    thread = get_or_create_campaign_thread(db, cid, created_by=owner)
    db.commit()
    tid = str(thread.id)
    accept_submission(
        db, campaign_id=cid, user_id=owner, raw_content="We sneak in",
        segments=[{"type": "ic", "text": "We sneak in."}], thread_id=tid,
    )
    db.commit()
    _turn, attempt = coordinate_turn(db, cid, tid)
    # Must not raise: scope-less private scene state projects as
    # dm_only/adjudication-only instead of failing ContextRecord validation.
    packet = assemble_attempt_context(
        db, attempt.id,
        supplemental_status={
            LaneName.KNOWLEDGE_VISIBILITY: "not_applicable",
            LaneName.CONTENT_BOUNDARIES: "not_applicable",
            LaneName.DIFFICULTY: "not_applicable",
        },
    )
    lane = next(
        record for record in packet.lanes if record.name == LaneName.CURRENT_SCENE
    )
    assert len(lane.records) == 1
    assert lane.records[0].visibility == "dm_only"
    assert lane.records[0].use == "adjudication_only"
    proj = packet.narration_projection()
    proj_lane = next(
        record for record in proj["lanes"] if record["name"] == "current_scene"
    )
    assert proj_lane["records"] == []


