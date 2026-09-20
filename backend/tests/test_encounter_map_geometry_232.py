"""Issue #232 — authoritative map geometry, placements, terrain, reachable movement."""
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
from app.combat.geometry import (  # noqa: E402
    GeometryError,
    cheapest_path,
    reachable_cells,
)
from app.combat.maps import (  # noqa: E402
    MOVED_EVENT,
    MAP_UPDATED_EVENT,
    MapError,
    ensure_map,
    find_move_by_operation,
    get_map,
    get_placement,
    map_projection,
    move_participant,
    reachable_for,
    update_terrain,
    update_terrain_inline,
)
from app.combat.service import (  # noqa: E402
    encounter_view,
    fulfill_human_initiative,
    get_snapshot_encounter,
    list_participants,
    roll_npc_initiative,
    start_encounter,
)
from app.combat.turns import StaleTurnError, get_turn_state_row  # noqa: E402
from app.dm.turns import coordinate_turn  # noqa: E402
from app.runtime.submissions import accept_submission  # noqa: E402
from app.runtime.threads import get_or_create_campaign_thread  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import Encounter, EncounterParticipant  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import WorldEntity  # noqa: E402


def _engine(url="sqlite://"):
    eng = create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    return eng


def _seed_world(db):
    owner = uuid.uuid4()
    player = uuid.uuid4()
    campaign_id = uuid.uuid4()
    owner_pc = uuid.uuid4()
    player_pc = uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=player, email="player@example.com"),
        Campaign(id=campaign_id, owner_id=owner, name="Map Table", revision=0),
        CampaignMember(campaign_id=campaign_id, user_id=owner, role="owner",
                       selected_character_id=owner_pc),
        CampaignMember(campaign_id=campaign_id, user_id=player, role="player",
                       selected_character_id=player_pc),
    ])
    db.flush()
    db.add_all([
        Character(id=owner_pc, owner_id=owner, name="Owner Blade", system="dnd5e"),
        Character(id=player_pc, owner_id=player, name="Player Bow", system="dnd5e"),
        Dnd5eCharacterSheet(character_id=owner_pc, owner_id=owner, character_name="Owner Blade",
                            dexterity=16, initiative_bonus=1, level=3),
        Dnd5eCharacterSheet(character_id=player_pc, owner_id=player, character_name="Player Bow",
                            dexterity=14, initiative_bonus=0, level=3),
    ])
    goblin = WorldEntity(campaign_id=campaign_id, entity_type="npc", name="Goblin Ambusher",
                         visibility="dm_only",
                         details={"initiative_modifier": 2, "dex_modifier": 1})
    db.add(goblin)
    db.commit()
    thread = get_or_create_campaign_thread(db, campaign_id, created_by=owner)
    submission = accept_submission(
        db, campaign_id=campaign_id, user_id=owner, character_id=owner_pc,
        raw_content="Goblins burst from the treeline!",
        segments=[{"type": "ic", "text": "Goblins burst from the treeline!"}],
        thread_id=str(thread.id),
    )
    db.commit()
    turn, attempt = coordinate_turn(db, campaign_id, str(thread.id))
    return {
        "campaign_id": campaign_id, "thread_id": str(thread.id),
        "owner": owner, "player": player,
        "owner_pc": owner_pc, "player_pc": player_pc,
        "goblin_id": goblin.id, "turn_id": turn.id, "attempt_id": attempt.id,
    }


def _fixture():
    eng = _engine()
    fac = sessionmaker(bind=eng, expire_on_commit=False)
    with fac() as db:
        ctx = _seed_world(db)
    return fac, ctx


def _pc(db, encounter_id, character_id):
    return db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter_id,
            EncounterParticipant.character_id == character_id,
        )
    ).scalars().one()


def _active_solo(db, ctx, *, operation_id="op-enc-1", raw_d20=10):
    """Start a solo-PC encounter and drive it to active; returns (encounter, participant)."""
    encounter, _ = start_encounter(
        db, ctx["campaign_id"], operation_id=operation_id, expected_revision=0,
        actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
        source_attempt_id=ctx["attempt_id"],
        scene={"location_name": "Treeline"},
        participants=[{"character_id": str(ctx["owner_pc"])}],
    )
    participant = _pc(db, encounter.id, ctx["owner_pc"])
    _, _, _, encounter, _ = fulfill_human_initiative(
        db, encounter.id, participant.id, actor_id=ctx["owner"],
        payload={"source": "app", "raw_rolls": [raw_d20],
                 "modifier": participant.initiative_modifier,
                 "total": raw_d20 + participant.initiative_modifier},
    )
    assert encounter.status == "active"
    db.refresh(participant)
    return encounter, participant


def _map(db, ctx, encounter, participant, **kwargs):
    revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
    defaults = {
        "actor_id": ctx["owner"], "width": 10, "height": 10,
        "terrain": [],
        "placements": [{"participant_id": str(participant.id), "col": 0, "row": 0}],
        "expected_revision": revision, "operation_id": f"op-map-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)
    return ensure_map(db, encounter.id, **defaults)


def _move(db, ctx, encounter, participant, col, row, **kwargs):
    revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
    defaults = {
        "actor_id": ctx["owner"], "to_col": col, "to_row": row,
        "expected_turn_sequence": int(encounter.turn_sequence or 0),
        "expected_revision": revision,
        "operation_id": f"op-move-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)
    return move_participant(db, encounter.id, participant.id, **defaults)


# ── pure geometry: ported legacy movement cases ──────────────────────────────


def test_geometry_normal_path_costs_one_per_square():
    distances = reachable_cells(
        width=5, height=5, zones=[], start=(0, 0), max_squares=6,
    )
    assert distances[(0, 0)] == 0
    assert distances[(2, 0)] == 2
    assert distances[(1, 1)] == 1  # diagonal costs one square
    path, cost = cheapest_path(
        width=5, height=5, zones=[], start=(0, 0), goal=(3, 0), max_squares=6,
    )
    assert cost == 3
    assert path[0] == (0, 0) and path[-1] == (3, 0)


def test_geometry_blocked_cell_denies_entry():
    zones = [{"kind": "blocked", "rect": {"col": 1, "row": 0, "width": 1, "height": 1},
              "cost_multiplier": 1}]
    distances = reachable_cells(width=5, height=1, zones=zones, start=(0, 0), max_squares=6)
    assert (1, 0) not in distances
    assert (2, 0) not in distances  # 1-wide corridor: no way around
    with pytest.raises(GeometryError, match="blocked"):
        cheapest_path(width=5, height=1, zones=zones, start=(0, 0), goal=(1, 0), max_squares=6)


def test_geometry_difficult_terrain_costs_double():
    zones = [{"kind": "difficult", "rect": {"col": 1, "row": 0, "width": 2, "height": 1},
              "cost_multiplier": 2}]
    path, cost = cheapest_path(
        width=5, height=1, zones=zones, start=(0, 0), goal=(3, 0), max_squares=6,
    )
    assert cost == 5  # 2 + 2 + 1
    assert path[-1] == (3, 0)
    with pytest.raises(GeometryError, match="unreachable"):
        cheapest_path(width=5, height=1, zones=zones, start=(0, 0), goal=(3, 0), max_squares=4)


def test_geometry_diagonal_corner_policy_is_deterministic():
    zones = [
        {"kind": "blocked", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}, "cost_multiplier": 1},
        {"kind": "blocked", "rect": {"col": 0, "row": 1, "width": 1, "height": 1}, "cost_multiplier": 1},
    ]
    with pytest.raises(GeometryError, match="unreachable"):
        cheapest_path(width=3, height=3, zones=zones, start=(0, 0), goal=(1, 1),
                      max_squares=6, diagonal_policy="no_corner_cut")
    path, cost = cheapest_path(width=3, height=3, zones=zones, start=(0, 0), goal=(1, 1),
                               max_squares=6, diagonal_policy="allow_corner_cut")
    assert cost == 1 and path == [(0, 0), (1, 1)]
    with pytest.raises(GeometryError, match="diagonal_policy"):
        reachable_cells(width=3, height=3, zones=[], start=(0, 0), max_squares=2,
                        diagonal_policy="chebyshev")


def test_geometry_later_zones_win_and_open_clears():
    zones = [
        {"kind": "difficult", "rect": {"col": 0, "row": 0, "width": 3, "height": 1}, "cost_multiplier": 2},
        {"kind": "blocked", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}, "cost_multiplier": 1},
        {"kind": "open", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}, "cost_multiplier": 1},
    ]
    path, cost = cheapest_path(width=3, height=1, zones=zones, start=(0, 0), goal=(2, 0), max_squares=6)
    assert cost == 3  # open re-carve clears to cost 1; (2,0) still difficult x2


# ── authoritative state: init, move, budget ──────────────────────────────────


def test_map_init_is_durable_and_background_art_stays_opaque():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        encounter_map, event = _map(
            db, ctx, encounter, participant, width=8, height=6,
            background_art_ref="gen-art-123",
        )
        assert (encounter_map.width, encounter_map.height) == (8, 6)
        assert encounter_map.diagonal_policy == "no_corner_cut"
        assert encounter_map.revision == 1
        assert event.event_type == MAP_UPDATED_EVENT
        placement = get_placement(db, encounter.id, participant.id)
        assert (placement.col, placement.row) == (0, 0)
        # Reconnect read: snapshot carries the same authoritative geometry.
        snapshot = get_snapshot_encounter(db, ctx["campaign_id"], ctx["owner"])
        assert snapshot["map"]["width"] == 8
        assert snapshot["map"]["background_art_ref"] == "gen-art-123"
        assert snapshot["map"]["placements"][0]["col"] == 0


def test_normal_move_commits_position_and_budget_atomically():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        state = get_turn_state_row(db, encounter.id, participant.id)
        assert state.movement_remaining == 30

        move, updated, event = _move(db, ctx, encounter, participant, 3, 0,
                                     operation_id="op-move-normal")
        assert move.cost_squares == 3 and move.cost_feet == 15
        assert (move.to_col, move.to_row) == (3, 0)
        assert event.event_type == MOVED_EVENT
        placement = get_placement(db, encounter.id, participant.id)
        assert (placement.col, placement.row) == (3, 0)
        state = get_turn_state_row(db, encounter.id, participant.id)
        assert state.movement_remaining == 15
        assert find_move_by_operation(db, encounter.id, "op-move-normal").id == move.id


def test_difficult_terrain_move_spends_double():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant, terrain=[
            {"kind": "difficult", "rect": {"col": 1, "row": 0, "width": 2, "height": 10}},
        ])
        move, _, _ = _move(db, ctx, encounter, participant, 2, 0)
        assert move.cost_squares == 4 and move.cost_feet == 20
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 10


def test_blocked_destination_rejected_before_mutation():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant, terrain=[
            {"kind": "blocked", "rect": {"col": 2, "row": 0, "width": 1, "height": 1}},
        ])
        with pytest.raises(MapError, match="blocked") as excinfo:
            _move(db, ctx, encounter, participant, 2, 0)
        assert excinfo.value.reason == "blocked"
        # Failure moves nothing and consumes nothing.
        placement = get_placement(db, encounter.id, participant.id)
        assert (placement.col, placement.row) == (0, 0)
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30


def test_insufficient_movement_rejected_with_exact_reason():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        # 30 ft = 6 squares; (0,0) -> (8,0) needs 8.
        with pytest.raises(MapError, match="unreachable") as excinfo:
            _move(db, ctx, encounter, participant, 8, 0)
        assert excinfo.value.reason == "insufficient_movement"
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30
        assert get_placement(db, encounter.id, participant.id).col == 0


def test_out_of_bounds_rejected_before_mutation():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        with pytest.raises(MapError, match="out of bounds") as excinfo:
            _move(db, ctx, encounter, participant, 10, 0)
        assert excinfo.value.reason == "out_of_bounds"
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30


def test_occupied_destination_rejected():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        encounter_map, _ = _map(
            db, ctx, encounter, participant,
            placements=[{"participant_id": str(participant.id), "col": 0, "row": 0}],
        )
        # Second token stands at (1, 0): fabricate via a guest participant row.
        from models.combat import EncounterPlacement
        guest_id = uuid.uuid4()
        db.add(EncounterPlacement(
            encounter_id=encounter.id, campaign_id=ctx["campaign_id"],
            participant_id=guest_id, col=1, row=0,
        ))
        db.commit()
        with pytest.raises(MapError, match="occupied") as excinfo:
            _move(db, ctx, encounter, participant, 1, 0)
        assert excinfo.value.reason == "occupied"
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30


def test_diagonal_around_blocking_corners_rejected_under_launch_policy():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant, terrain=[
            {"kind": "blocked", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}},
            {"kind": "blocked", "rect": {"col": 0, "row": 1, "width": 1, "height": 1}},
        ])
        with pytest.raises(MapError) as excinfo:
            _move(db, ctx, encounter, participant, 1, 1)
        assert excinfo.value.reason == "unreachable"
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30


def test_stale_turn_sequence_cannot_spend():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        with pytest.raises(StaleTurnError):
            _move(db, ctx, encounter, participant, 2, 0, expected_turn_sequence=999)
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 30


def test_unsupported_movement_mode_rejected():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        with pytest.raises(MapError, match="movement_mode") as excinfo:
            _move(db, ctx, encounter, participant, 2, 0, movement_mode="fly")
        assert excinfo.value.reason == "unsupported_mode"


# ── DM terrain changes reshape subsequent reachable space ────────────────────


def test_dm_terrain_change_affects_subsequent_reachable_calc():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        before = reachable_for(db, encounter.id, participant.id)
        assert any(c["col"] == 5 and c["row"] == 0 for c in before["cells"])

        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        encounter_map, event = update_terrain(
            db, encounter.id, actor_id=ctx["owner"],
            zones=[{"kind": "blocked", "rect": {"col": 0, "row": 0, "width": 10, "height": 1},
                    "label": "Wall of thorns"}],
            expected_revision=revision, operation_id="op-terrain-wall",
        )
        assert event.event_type == MAP_UPDATED_EVENT
        assert encounter_map.revision == 2
        after = reachable_for(db, encounter.id, participant.id)
        # The walled row is impassable along itself, but the stranded token
        # can still escape off the wall.
        assert all(not (c["col"] == 5 and c["row"] == 0) for c in after["cells"])
        assert any(c["col"] == 0 and c["row"] == 1 for c in after["cells"])
        with pytest.raises(MapError, match="blocked"):
            _move(db, ctx, encounter, participant, 5, 0)
        escape, _, _ = _move(db, ctx, encounter, participant, 0, 1)
        assert (escape.to_col, escape.to_row) == (0, 1)


def test_dm_terrain_inline_effect_path_validates_and_bumps_revision():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        campaign = db.get(Campaign, ctx["campaign_id"])
        encounter_map = update_terrain_inline(
            db, campaign, encounter,
            {"zones": [{"kind": "difficult",
                        "rect": {"col": 1, "row": 0, "width": 3, "height": 10}}]},
            "op-inline-terrain",
        )
        assert encounter_map.revision == 2
        move, _, _ = _move(db, ctx, encounter, participant, 2, 0)
        assert move.cost_squares == 4
        with pytest.raises(MapError):
            update_terrain_inline(db, campaign, encounter, {"zones": "not-a-list"}, "op-bad")


# ── idempotency, reconnect, visibility ────────────────────────────────────────


def test_duplicate_move_command_cannot_double_spend():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        first, _, _ = _move(db, ctx, encounter, participant, 2, 0, operation_id="op-move-dup")
        # Same operation retried — even against a *different* destination the
        # recorded outcome replays instead of moving again.
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        replay, _, event = move_participant(
            db, encounter.id, participant.id, actor_id=ctx["owner"],
            to_col=4, to_row=0,
            expected_turn_sequence=int(encounter.turn_sequence or 0),
            expected_revision=revision, operation_id="op-move-dup",
        )
        assert replay.id == first.id
        assert event is None
        assert (replay.to_col, replay.to_row) == (2, 0)
        placement = get_placement(db, encounter.id, participant.id)
        assert (placement.col, placement.row) == (2, 0)
        assert get_turn_state_row(db, encounter.id, participant.id).movement_remaining == 20


def test_reconnect_reconstructs_positions_and_terrain_from_backend():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant, terrain=[
            {"kind": "difficult", "rect": {"col": 3, "row": 3, "width": 2, "height": 2},
             "label": "Rubble"},
        ])
        _move(db, ctx, encounter, participant, 2, 1)
    # Fresh session: no in-memory state, rebuild purely from durable rows.
    with fac() as db2:
        snapshot = get_snapshot_encounter(db2, ctx["campaign_id"], ctx["owner"])
        assert snapshot is not None
        assert snapshot["map"]["width"] == 10
        assert len(snapshot["map"]["zones"]) == 1
        assert snapshot["map"]["placements"][0]["col"] == 2
        view = encounter_view(
            db2, db2.get(Encounter, uuid.UUID(snapshot["id"])),
            ctx["owner"], is_owner=True,
        )
        assert view["map"]["placements"][0]["row"] == 1
        assert view["turn"]["resources"][str(participant.id)]["movement_remaining"] == 20
        assert view["map"]["zones"][0]["kind"] == "difficult"


def test_projection_hides_dm_labels_and_hidden_npc_tokens():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-enc-1", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
            source_attempt_id=ctx["attempt_id"],
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"npc_entity_id": str(ctx["goblin_id"])}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        goblin_p = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == ctx["goblin_id"],
            )
        ).scalars().one()
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [10],
                     "modifier": owner_p.initiative_modifier,
                     "total": 10 + owner_p.initiative_modifier},
        )
        roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=5)
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        ensure_map(
            db, encounter.id, actor_id=ctx["owner"], width=6, height=6,
            terrain=[{"kind": "blocked",
                      "rect": {"col": 2, "row": 2, "width": 1, "height": 1},
                      "label": "Secret pit trap", "visibility": "dm_only"}],
            placements=[{"participant_id": str(owner_p.id), "col": 0, "row": 0},
                        {"participant_id": str(goblin_p.id), "col": 5, "row": 5}],
            expected_revision=revision, operation_id="op-map-hidden",
        )
        owner_view = map_projection(db, encounter, viewer_id=ctx["owner"], is_owner=True)
        assert owner_view["zones"][0]["label"] == "Secret pit trap"
        assert len(owner_view["placements"]) == 2

        player_view = map_projection(db, encounter, viewer_id=ctx["player"], is_owner=False)
        assert "label" not in player_view["zones"][0]  # DM-only label never leaks
        assert player_view["zones"][0]["kind"] == "blocked"  # mechanics stay shared
        kinds = [p["kind"] for p in player_view["placements"]]
        assert "npc" not in kinds and "monster" not in kinds  # hidden token hook
        assert len(player_view["placements"]) == 1


def test_non_owner_cannot_define_geometry_or_terrain():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        from app.combat.maps import MapAuthorizationError
        with pytest.raises(MapAuthorizationError):
            ensure_map(db, encounter.id, actor_id=ctx["player"], width=6, height=6,
                       expected_revision=revision, operation_id="op-map-nope")
        assert get_map(db, encounter.id) is None


def test_reachable_read_is_safe_for_any_reader_and_reports_budget():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        result = reachable_for(db, encounter.id, participant.id)
        assert result["movement_remaining_ft"] == 30
        assert result["max_squares"] == 6
        assert result["map_revision"] == 1
        # No DM-only detail leaks through the coordinate/cost shape.
        assert all(set(c) == {"col", "row", "cost_squares", "cost_feet"} for c in result["cells"])


# ── AI review round 1 (#404): hidden-token, ordering, redefine, dedupe ───────


def _active_duo(db, ctx, *, pc="owner_pc", actor="owner", pc_roll=1, goblin_roll=20,
               operation_id=None):
    """Two-token active encounter where the goblin holds the turn by default.

    Pass pc_roll high / goblin_roll low to put the PC on turn instead.
    """
    encounter, _ = start_encounter(
        db, ctx["campaign_id"], operation_id=operation_id or f"op-enc-duo-{uuid.uuid4().hex[:8]}",
        expected_revision=0,
        actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
        source_attempt_id=ctx["attempt_id"],
        participants=[{"character_id": str(ctx[pc])},
                      {"npc_entity_id": str(ctx["goblin_id"])}],
    )
    pc_p = _pc(db, encounter.id, ctx[pc])
    goblin_p = db.execute(
        select(EncounterParticipant).where(
            EncounterParticipant.encounter_id == encounter.id,
            EncounterParticipant.npc_entity_id == ctx["goblin_id"],
        )
    ).scalars().one()
    fulfill_human_initiative(
        db, encounter.id, pc_p.id, actor_id=ctx[actor],
        payload={"source": "app", "raw_rolls": [pc_roll],
                 "modifier": pc_p.initiative_modifier,
                 "total": pc_roll + pc_p.initiative_modifier},
    )
    roll_npc_initiative(db, encounter.id, goblin_p.id, raw_d20=goblin_roll)
    db.refresh(encounter)
    assert encounter.status == "active"
    return encounter, pc_p, goblin_p


def _duo_map(db, ctx, encounter, pc_p, goblin_p, **kwargs):
    revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
    defaults = {
        "actor_id": ctx["owner"], "width": 6, "height": 6,
        "terrain": [],
        "placements": [{"participant_id": str(pc_p.id), "col": 0, "row": 0},
                       {"participant_id": str(goblin_p.id), "col": 5, "row": 5}],
        "expected_revision": revision,
        "operation_id": f"op-duo-map-{uuid.uuid4().hex[:8]}",
    }
    defaults.update(kwargs)
    return ensure_map(db, encounter.id, **defaults)


def test_hidden_npc_reachable_denied_for_non_owner():
    from app.combat.maps import MapAuthorizationError
    fac, ctx = _fixture()
    with fac() as db:
        encounter, owner_p, goblin_p = _active_duo(db, ctx)
        _duo_map(db, ctx, encounter, owner_p, goblin_p)
        # Owner reads the hidden token's reachable space.
        seen = reachable_for(db, encounter.id, goblin_p.id,
                             viewer_id=ctx["owner"], is_owner=True)
        assert seen["from"] == {"col": 5, "row": 5}
        # Non-owner probing the hidden token gets 403, not coordinates.
        with pytest.raises(MapAuthorizationError):
            reachable_for(db, encounter.id, goblin_p.id,
                          viewer_id=ctx["player"], is_owner=False)
        # Ordinary PC reads still work for any thread reader.
        pc_seen = reachable_for(db, encounter.id, owner_p.id,
                                viewer_id=ctx["player"], is_owner=False)
        assert pc_seen["from"] == {"col": 0, "row": 0}


def test_hidden_stranded_placements_filtered_for_non_owner():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, owner_p, goblin_p = _active_duo(db, ctx)
        _duo_map(db, ctx, encounter, owner_p, goblin_p)
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        update_terrain(
            db, encounter.id, actor_id=ctx["owner"],
            zones=[{"kind": "blocked", "rect": {"col": 5, "row": 5, "width": 1, "height": 1},
                    "label": "Secret pit", "visibility": "dm_only"}],
            expected_revision=revision, operation_id="op-strand-hidden",
        )
        owner_view = map_projection(db, encounter, viewer_id=ctx["owner"], is_owner=True)
        assert any(s["participant_id"] == str(goblin_p.id)
                   for s in owner_view["stranded_placements"])
        player_view = map_projection(db, encounter, viewer_id=ctx["player"], is_owner=False)
        # The stranded flag carries the exact cell: hidden tokens stay out.
        assert all(s["participant_id"] != str(goblin_p.id)
                   for s in player_view["stranded_placements"])


def test_overlapping_zones_preserve_authored_order():
    from app.combat.maps import list_zones
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant, width=3, height=1, terrain=[
            {"kind": "blocked", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}},
            {"kind": "open", "rect": {"col": 1, "row": 0, "width": 1, "height": 1}},
        ])
        # Same-transaction rows share created_at: only the explicit ordinal
        # keeps authored order deterministic on read-back.
        zones = list_zones(db, get_map(db, encounter.id).id)
        assert [z.kind for z in zones] == ["blocked", "open"]
        assert [int(z.zone_order or 0) for z in zones] == [0, 1]
        # Later-wins semantics hold through the persisted read path.
        move, _, _ = _move(db, ctx, encounter, participant, 2, 0)
        assert (move.to_col, move.to_row) == (2, 0)


def test_map_redefine_without_placements_preserves_positions():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        encounter_map, _ = _map(db, ctx, encounter, participant)
        assert encounter_map.revision == 1
        _move(db, ctx, encounter, participant, 3, 0, operation_id="op-preserve-move")
        # Redefine geometry/art without placements: position survives.
        kept, _ = _map(db, ctx, encounter, participant,
                       background_art_ref="gen-art-2", placements=None,
                       operation_id="op-redefine-keep")
        assert kept.revision == 2
        assert (get_placement(db, encounter.id, participant.id).col,
                get_placement(db, encounter.id, participant.id).row) == (3, 0)
        # A shrink that would push the token off-grid rejects instead.
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        with pytest.raises(MapError, match="outside the replacement") as excinfo:
            ensure_map(db, encounter.id, actor_id=ctx["owner"], width=2, height=2,
                       placements=None, expected_revision=revision,
                       operation_id="op-redefine-shrink")
        assert excinfo.value.reason == "out_of_bounds"
        db.rollback()
        assert (get_placement(db, encounter.id, participant.id).col,
                get_placement(db, encounter.id, participant.id).row) == (3, 0)


def test_two_moves_same_turn_have_distinct_realtime_event_ids():
    from app.realtime.service import (
        InMemoryRealtimePublisher,
        build_encounter_moved_event,
        dedupe_events,
        set_realtime_publisher,
    )
    fac, ctx = _fixture()
    pub = InMemoryRealtimePublisher()
    set_realtime_publisher(pub)
    try:
        with fac() as db:
            encounter, participant = _active_solo(db, ctx)
            _map(db, ctx, encounter, participant)
            first, _, _ = _move(db, ctx, encounter, participant, 1, 0,
                                operation_id="op-rt-move-1")
            second, _, _ = _move(db, ctx, encounter, participant, 2, 0,
                                 operation_id="op-rt-move-2")
            moved = [r for r in pub.published if r["event"] == "encounter.moved"]
            assert len(moved) == 2
            ids = [m["payload"]["event_id"] for m in moved]
            assert ids[0] != ids[1]
            assert ids[0].endswith(str(first.id)) and ids[1].endswith(str(second.id))
            # The realtime deduper must keep both same-turn moves.
            assert len(dedupe_events([m["payload"] for m in moved])) == 2
            # Legacy callers without a move id keep the turn-scoped key.
            legacy = build_encounter_moved_event(encounter, participant.id)
            assert legacy["event_id"].endswith(f"{int(encounter.turn_sequence or 0)}")
    finally:
        set_realtime_publisher(None)


def test_hidden_mover_move_redacts_positions():
    from app.realtime.service import InMemoryRealtimePublisher, set_realtime_publisher
    fac, ctx = _fixture()
    pub = InMemoryRealtimePublisher()
    set_realtime_publisher(pub)
    try:
        with fac() as db:
            encounter, owner_p, goblin_p = _active_duo(db, ctx)
            assert encounter.active_participant_id == goblin_p.id
            _duo_map(db, ctx, encounter, owner_p, goblin_p)
            move, _, event = _move(db, ctx, encounter, goblin_p, 4, 5,
                                   operation_id="op-hidden-move")
            # Authoritative state still commits exactly.
            assert (move.to_col, move.to_row) == (4, 5)
            assert (get_placement(db, encounter.id, goblin_p.id).col,
                    get_placement(db, encounter.id, goblin_p.id).row) == (4, 5)
            # Durable thread-scoped event carries no hidden coordinates.
            assert "from" not in event.payload and "to" not in event.payload
            assert event.payload.get("position_redacted") is True
            # Shared realtime payload is a position-free invalidation.
            moved = [r for r in pub.published if r["event"] == "encounter.moved"]
            assert len(moved) == 1
            assert moved[0]["payload"]["to"] == {}
            assert moved[0]["payload"].get("position_redacted") is True
    finally:
        set_realtime_publisher(None)


# ── AI review round 2 (#404): entity-visibility signal, shared events ────────


def test_public_npc_token_stays_visible_despite_private_stats():
    """Stat privacy (#230) is not token hiding: a campaign-visible NPC keeps
    its token, reachable reads, and movement for non-owners."""
    fac, ctx = _fixture()
    with fac() as db:
        guard = WorldEntity(campaign_id=ctx["campaign_id"], entity_type="npc",
                            name="Town Guard", visibility="campaign",
                            details={"initiative_modifier": 1, "dex_modifier": 1})
        db.add(guard)
        db.commit()
        encounter, _ = start_encounter(
            db, ctx["campaign_id"], operation_id="op-enc-guard", expected_revision=0,
            actor_id=ctx["owner"], source_turn_id=ctx["turn_id"],
            source_attempt_id=ctx["attempt_id"],
            participants=[{"character_id": str(ctx["owner_pc"])},
                          {"npc_entity_id": str(guard.id)}],
        )
        owner_p = _pc(db, encounter.id, ctx["owner_pc"])
        guard_p = db.execute(
            select(EncounterParticipant).where(
                EncounterParticipant.encounter_id == encounter.id,
                EncounterParticipant.npc_entity_id == guard.id,
            )
        ).scalars().one()
        fulfill_human_initiative(
            db, encounter.id, owner_p.id, actor_id=ctx["owner"],
            payload={"source": "app", "raw_rolls": [10],
                     "modifier": owner_p.initiative_modifier,
                     "total": 10 + owner_p.initiative_modifier},
        )
        roll_npc_initiative(db, encounter.id, guard_p.id, raw_d20=5)
        # Stats stay DM-private per #230 even though the token is public.
        assert guard_p.stat_visibility == "dm_private"
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        ensure_map(
            db, encounter.id, actor_id=ctx["owner"], width=6, height=6,
            placements=[{"participant_id": str(owner_p.id), "col": 0, "row": 0},
                        {"participant_id": str(guard_p.id), "col": 5, "row": 0}],
            expected_revision=revision, operation_id="op-map-guard",
        )
        player_view = map_projection(db, encounter, viewer_id=ctx["player"], is_owner=False)
        assert any(p["participant_id"] == str(guard_p.id)
                   for p in player_view["placements"])
        seen = reachable_for(db, encounter.id, guard_p.id,
                             viewer_id=ctx["player"], is_owner=False)
        assert seen["from"] == {"col": 5, "row": 0}


def test_shared_terrain_event_omits_hidden_stranded():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, owner_p, goblin_p = _active_duo(db, ctx)
        _duo_map(db, ctx, encounter, owner_p, goblin_p)
        revision = int(db.get(Campaign, ctx["campaign_id"]).revision or 0)
        _, event = update_terrain(
            db, encounter.id, actor_id=ctx["owner"],
            zones=[{"kind": "blocked", "rect": {"col": 5, "row": 5, "width": 1, "height": 1}}],
            expected_revision=revision, operation_id="op-event-strand",
        )
        # Thread-scoped history must not carry the hidden token's cell.
        assert event.payload["stranded_placements"] == []
        # Owner projection still flags it (ledger + owner views intact).
        owner_view = map_projection(db, encounter, viewer_id=ctx["owner"], is_owner=True)
        assert any(s["participant_id"] == str(goblin_p.id)
                   for s in owner_view["stranded_placements"])


def test_non_owner_preview_and_commit_share_hidden_occupancy():
    """Preview/commit consistency: hidden tokens neither carve the
    non-owner reachable shape nor block that actor's commits, so probing a
    hidden cell reveals nothing (no reachable-yet-unreachable oracle).
    Owners keep the full authoritative collision."""
    fac, ctx = _fixture()
    with fac() as db:
        encounter, player_p, goblin_p = _active_duo(
            db, ctx, pc="player_pc", actor="player", pc_roll=20, goblin_roll=1,
            operation_id="op-enc-probe",
        )
        assert encounter.active_participant_id == player_p.id
        _duo_map(db, ctx, encounter, player_p, goblin_p)
        # Owner's reachable folds every token; the non-owner shape is not
        # carved by the hidden goblin at (5, 5).
        owner_cells = {(c["col"], c["row"]) for c in reachable_for(
            db, encounter.id, player_p.id, viewer_id=ctx["owner"], is_owner=True)["cells"]}
        player_cells = {(c["col"], c["row"]) for c in reachable_for(
            db, encounter.id, player_p.id, viewer_id=ctx["player"], is_owner=False)["cells"]}
        assert (5, 5) not in owner_cells
        assert (5, 5) in player_cells
        # The commit agrees with the preview: stepping onto the hidden cell
        # as a non-owner succeeds exactly as previewed (no oracle).
        move, _, _ = _move(db, ctx, encounter, player_p, 5, 5,
                           actor_id=ctx["player"], operation_id="op-probe-hidden")
        assert (move.to_col, move.to_row) == (5, 5)
        assert (get_placement(db, encounter.id, player_p.id).col,
                get_placement(db, encounter.id, player_p.id).row) == (5, 5)
        # Same destination as the owner (who sees the hidden token) still
        # collides: isolate the goblin's cell at the geometry level.
        from app.combat.maps import _occupied_cells
        assert (5, 5) in _occupied_cells(db, encounter.id,
                                         exclude_participant_id=player_p.id,
                                         include_hidden=True)
        assert (5, 5) not in _occupied_cells(db, encounter.id,
                                             exclude_participant_id=player_p.id,
                                             include_hidden=False)


def test_staged_terrain_effect_promotes_through_turn_pipeline_and_bumps_revision():
    """DM staged-effect registration (#232 blocker): a staged
    update_map_terrain effect promotes through the #206 turn-commit
    pipeline and bumps the map revision."""
    from datetime import datetime, timezone

    from app.dm.contract import normalize_contract
    from app.dm.turns import commit_turn_with_effects, mark_streaming_started, stage_validated_attempt
    from models.dm import DMStream, DMStreamChunk

    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        assert get_map(db, encounter.id).revision == 1

        accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["owner"],
            character_id=ctx["owner_pc"],
            raw_content="Thorns erupt across the treeline!",
            segments=[{"type": "ic", "text": "Thorns erupt across the treeline!"}],
            thread_id=ctx["thread_id"],
        )
        db.commit()
        turn, attempt = coordinate_turn(db, ctx["campaign_id"], ctx["thread_id"])
        db.commit()
        contract = normalize_contract({
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond",
            "reason": "dm reshapes terrain",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "Thorns erupt!", "claim_kind": "observation",
                            "origin": "dm_adjudication"}],
            }],
            "staged_effects": [{
                "id": "terrain-1", "effect_type": "update_map_terrain",
                "arguments": {
                    "encounter_id": str(encounter.id),
                    "zones": [{"kind": "difficult",
                               "rect": {"col": 1, "row": 0, "width": 3, "height": 10}}],
                },
            }],
        })
        stage_validated_attempt(db, attempt.id, contract)
        stream = DMStream(
            id=uuid.uuid4(), campaign_id=turn.campaign_id,
            thread_id=uuid.UUID(str(turn.thread_id)),
            turn_id=str(turn.id), attempt_id=str(attempt.id),
            status="streaming", audience=turn.audience,
        )
        db.add(stream)
        db.flush()
        text = "Thorns erupt across the treeline!"
        db.add(DMStreamChunk(
            id=uuid.uuid4(), stream_id=stream.id, sequence=0,
            text=text, byte_length=len(text.encode()),
        ))
        stream.first_chunk_at = datetime.now(timezone.utc)
        stream.chunk_count = 1
        db.flush()
        mark_streaming_started(db, turn.id, attempt.id, stream.id)
        commit_turn_with_effects(db, turn.id, attempt.id)

        encounter_map = get_map(db, encounter.id)
        assert encounter_map.revision == 2
        move, _, _ = _move(db, ctx, encounter, participant, 2, 0)
        assert move.cost_squares == 4


def test_staged_placement_effect_promotes_through_turn_pipeline_and_bumps_revision():
    """DM staged-effect registration (#232 follow-up): a staged
    update_map_placement effect promotes through the #206 turn-commit
    pipeline, moves the token, and bumps the map revision."""
    from datetime import datetime, timezone

    from app.dm.contract import normalize_contract
    from app.dm.turns import commit_turn_with_effects, mark_streaming_started, stage_validated_attempt
    from models.dm import DMStream, DMStreamChunk

    fac, ctx = _fixture()
    with fac() as db:
        encounter, participant = _active_solo(db, ctx)
        _map(db, ctx, encounter, participant)
        assert get_map(db, encounter.id).revision == 1
        assert (get_placement(db, encounter.id, participant.id).col,
                get_placement(db, encounter.id, participant.id).row) == (0, 0)

        accept_submission(
            db, campaign_id=ctx["campaign_id"], user_id=ctx["owner"],
            character_id=ctx["owner_pc"],
            raw_content="The blade repositions to higher ground!",
            segments=[{"type": "ic", "text": "The blade repositions to higher ground!"}],
            thread_id=ctx["thread_id"],
        )
        db.commit()
        turn, attempt = coordinate_turn(db, ctx["campaign_id"], ctx["thread_id"])
        db.commit()
        contract = normalize_contract({
            "contract_version": "dm_turn_contract_v1",
            "mode": "respond",
            "reason": "dm repositions token",
            "beats": [{
                "id": "beat_1", "type": "narration",
                "claims": [{"text": "The blade moves!", "claim_kind": "observation",
                            "origin": "dm_adjudication"}],
            }],
            "staged_effects": [{
                "id": "placement-1", "effect_type": "update_map_placement",
                "arguments": {
                    "encounter_id": str(encounter.id),
                    "placements": [{"participant_id": str(participant.id),
                                    "col": 3, "row": 4}],
                },
            }],
        })
        stage_validated_attempt(db, attempt.id, contract)
        stream = DMStream(
            id=uuid.uuid4(), campaign_id=turn.campaign_id,
            thread_id=uuid.UUID(str(turn.thread_id)),
            turn_id=str(turn.id), attempt_id=str(attempt.id),
            status="streaming", audience=turn.audience,
        )
        db.add(stream)
        db.flush()
        text = "The blade repositions to higher ground!"
        db.add(DMStreamChunk(
            id=uuid.uuid4(), stream_id=stream.id, sequence=0,
            text=text, byte_length=len(text.encode()),
        ))
        stream.first_chunk_at = datetime.now(timezone.utc)
        stream.chunk_count = 1
        db.flush()
        mark_streaming_started(db, turn.id, attempt.id, stream.id)
        commit_turn_with_effects(db, turn.id, attempt.id)

        assert get_map(db, encounter.id).revision == 2
        placed = get_placement(db, encounter.id, participant.id)
        assert (placed.col, placed.row) == (3, 4)
        seen = reachable_for(db, encounter.id, participant.id)
        assert seen["from"] == {"col": 3, "row": 4}
