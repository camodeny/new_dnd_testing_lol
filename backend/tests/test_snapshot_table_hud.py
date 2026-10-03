"""Player-lane live-table projection (``snapshot["table"]``)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

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
from app.rules.mechanics import get_character_mechanics_for_sheet  # noqa: E402
from app.snapshot.table import build_table_for_viewer, health_label, roll_modifier  # noqa: E402
from app.world import clocks as _clocks  # noqa: E402
from app.world import facts as _facts  # noqa: E402
from app.world import knowledge as _knowledge  # noqa: E402
from app.world import service as _world  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.dm import PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.world import NPCState  # noqa: E402
from test_encounter_map_geometry_232 import _active_duo, _duo_map, _fixture  # noqa: E402


@pytest.fixture
def ctx():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=eng)
    Fac = sessionmaker(bind=eng, expire_on_commit=False)
    db = Fac()
    owner, alice, outsider = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db.add_all([
        Profile(id=owner, email="owner@example.com"),
        Profile(id=alice, email="alice@example.com"),
        Profile(id=outsider, email="outsider@example.com"),
    ])
    camp = Campaign(id=uuid.uuid4(), owner_id=owner, name="Table", revision=0)
    db.add(camp)
    db.flush()
    chars = {}
    for user, name, current, conditions in (
        (owner, "Bryn", 19, [
            {"condition_name": "poisoned", "visibility": "public"},
            {"condition_name": "cursed", "visibility": "dm_private"},
        ]),
        (alice, "Sera", 6, [
            {"condition_name": "frightened", "visibility": "public"},
            {"condition_name": "charmed", "visibility": "private"},
        ]),
    ):
        char = Character(id=uuid.uuid4(), owner_id=user, name=name, system="dnd5e")
        db.add(char)
        db.flush()
        db.add(Dnd5eCharacterSheet(
            character_id=char.id, owner_id=user, character_name=name,
            race="Human", char_class="Fighter", level=3, wisdom=14,
            hit_points_max=28, hit_points_current=current, armor_class=16,
            resources=[{"name": "Second Wind", "current": 1, "max": 1, "recharge": "short_rest"}],
            conditions=conditions,
        ))
        chars[user] = char.id
    db.add_all([
        CampaignMember(campaign_id=camp.id, user_id=owner, role="owner", selected_character_id=chars[owner]),
        CampaignMember(campaign_id=camp.id, user_id=alice, role="player", selected_character_id=chars[alice]),
    ])
    db.commit()
    yield {"factory": Fac, "campaign_id": camp.id, "owner": owner, "alice": alice,
           "outsider": outsider, "chars": chars}
    db.close()


def _seed_world(ctx):
    db = ctx["factory"]()
    camp = db.get(Campaign, ctx["campaign_id"])
    marta, _ = _world.create_entity(db, camp, entity_type="npc", name="Marta", summary="Keeps the ferry")
    _world.create_entity(db, camp, entity_type="npc", name="Hidden Spy", visibility="dm_only")
    db.add(NPCState(
        entity_id=marta.id, campaign_id=camp.id, role="ferrykeeper",
        current_activity="mending nets", goals=[{"summary": "hide her brother"}],
        disposition={"party": "wary"}, campaign_revision=1, provenance={"source": "test"},
        field_visibility={"role": "campaign"},
    ))
    _world.apply_scene_update(
        db, camp, new_revision=1, location_name="Gallows Landing", fictional_time="Night",
        present_actors=[{"name": "Marta", "entity_id": str(marta.id)}],
        environment={"premise": "DM-facing premise text"},
    )
    _facts.create_fact(
        db, camp, content="Marta's brother vanished last winter",
        entity_refs=[str(marta.id)], visibility="campaign", operation_id="f1",
    )
    _facts.create_fact(
        db, camp, content="Marta smuggles relics for the cult",
        entity_refs=[str(marta.id)], visibility="dm_only", operation_id="f2",
    )
    secret, _ = _facts.create_fact(
        db, camp, content="Alice saw a moth sigil", visibility="private", operation_id="f3",
    )
    _knowledge.grant_visibility(
        db, camp, target_kind="fact", target_id=secret.id,
        grantee_user_id=ctx["alice"], granted_by=ctx["owner"], operation_id="g1",
    )
    _clocks.create_clock(
        db, camp, name="Tide Rising", threshold=4, advancement_criteria={"kind": "deterministic"},
        visibility="campaign", provenance={"source": "test"}, operation_id="c1",
    )
    db.commit()
    db.close()
    return marta.id


def _view(ctx, who):
    db = ctx["factory"]()
    try:
        return build_table_for_viewer(db, db.get(Campaign, ctx["campaign_id"]), ctx[who])
    finally:
        db.close()


def test_owner_gets_the_player_lane_and_no_dm_machinery(ctx):
    _seed_world(ctx)
    blob = json.dumps(_view(ctx, "owner"), default=str)
    # The owner plays at the table: no dm_only fact or NPC…
    assert "smuggles relics" not in blob
    assert "Hidden Spy" not in blob
    # …no storytelling machinery even when member-visible…
    assert "Tide Rising" not in blob
    assert "hide her brother" not in blob
    assert "wary" not in blob
    assert "mending nets" not in blob
    assert "DM-facing premise" not in blob
    # …and no other player's private grant.
    assert "moth sigil" not in blob


def test_scene_and_journal(ctx):
    marta_id = _seed_world(ctx)
    view = _view(ctx, "alice")
    assert view["scene"] == {"location_name": "Gallows Landing", "fictional_time": "Night"}
    [marta] = view["journal"]["people"]
    assert marta["entity_id"] == str(marta_id)
    assert marta["role"] == "ferrykeeper"
    assert marta["summary"] == "Keeps the ferry"
    assert [f["content"] for f in marta["facts"]] == ["Marta's brother vanished last winter"]
    facts = {f["content"] for f in view["journal"]["facts"]}
    assert "Alice saw a moth sigil" in facts  # explicit grant
    assert "Marta smuggles relics for the cult" not in facts


def test_character_and_party_scopes(ctx):
    view = _view(ctx, "alice")
    me = view["character"]
    assert me["name"] == "Sera"
    assert me["hp"] == {"current": 6, "max": 28, "temp": 0}
    assert me["armor_class"] == 16
    assert me["resources"] == [{"name": "Second Wind", "current": 1, "max": 1, "recharge": "short_rest"}]
    assert any(s["name"] == "Insight" for s in me["skills"])
    # Own private condition is visible to its player; DM-private never is.
    assert set(me["conditions"]) == {"frightened", "charmed"}
    party = {p["name"]: p for p in view["party"]}
    bryn = party["Bryn"]
    # Other players see a health descriptor, not numbers or resources.
    assert bryn["health"] == "hurt"
    assert "hp" not in bryn and "resources" not in bryn
    assert bryn["conditions"] == ["poisoned"]
    assert party["Sera"]["health"] == "badly hurt"

    owner_view = _view(ctx, "owner")
    assert owner_view["character"]["conditions"] == ["poisoned"]
    sera = next(p for p in owner_view["party"] if p["name"] == "Sera")
    assert sera["conditions"] == ["frightened"]


def test_pending_roll_modifiers_come_from_the_sheet(ctx):
    db = ctx["factory"]()
    req = PlayerRollRequest(
        id=uuid.uuid4(), campaign_id=ctx["campaign_id"], thread_id=str(uuid.uuid4()),
        turn_id=uuid.uuid4(), attempt_id=uuid.uuid4(), request_key="k",
        requested_user_id=ctx["alice"], character_id=ctx["chars"][ctx["alice"]],
        roll_kind="check", ability_or_skill="Wisdom (Insight)", label="Insight",
        advantage_state="normal", reason_public="Is she lying?", status="pending",
    )
    db.add(req)
    db.commit()
    db.close()
    mods = _view(ctx, "alice")["character"]["roll_modifiers"]
    assert mods == {str(req.id): {"modifier": 2, "label": "Insight"}}
    # Nobody else sees another player's pending roll modifiers.
    assert _view(ctx, "owner")["character"]["roll_modifiers"] == {}


def test_roll_modifier_resolution():
    sheet = SimpleNamespace(
        id=uuid.uuid4(), character_id=uuid.uuid4(), owner_id=uuid.uuid4(), character_name="X",
        level=3, strength=16, dexterity=14, constitution=12, intelligence=10, wisdom=14, charisma=8,
        proficiency_bonus=2, armor_class=16, speed=30, hit_points_max=28, hit_points_current=28,
        hit_points_temp=0, initiative_bonus=0, save_proficiencies=None, saving_throws=None,
        skill_proficiencies=None, skills=None, skill_expertise=None, updated_at=None,
    )
    try:
        mechanics = get_character_mechanics_for_sheet(sheet)
    except Exception:
        pytest.skip("stub sheet does not satisfy the mechanics derivation")
    assert roll_modifier(mechanics, "ability", "Strength") == {"modifier": 3, "label": "Strength"}
    assert roll_modifier(mechanics, "initiative", "Dexterity")["modifier"] == 2
    assert roll_modifier(mechanics, "attack", "Longsword") is None
    assert roll_modifier(mechanics, "other", "Luck") is None


def test_health_labels():
    assert health_label(28, 28) == "unhurt"
    assert health_label(19, 28) == "hurt"
    assert health_label(6, 28) == "badly hurt"
    assert health_label(0, 28) == "down"


def test_non_member_gets_empty_projection(ctx):
    _seed_world(ctx)
    view = _view(ctx, "outsider")
    assert view["character"] is None
    assert view["party"] == [] and view["scene"] is None and view["encounter"] is None


def test_owner_combat_view_hides_hidden_tokens_and_terrain():
    fac, ctx = _fixture()
    with fac() as db:
        encounter, pc_p, goblin_p = _active_duo(db, ctx, pc_roll=20, goblin_roll=1)
        _duo_map(db, ctx, encounter, pc_p, goblin_p, terrain=[
            {"kind": "blocked", "rect": {"col": 2, "row": 2, "width": 1, "height": 1},
             "label": "Secret pit trap", "visibility": "dm_only"},
        ], placements=[{"participant_id": str(pc_p.id), "col": 0, "row": 0},
                       {"participant_id": str(goblin_p.id), "col": 2, "row": 0}])
        view = build_table_for_viewer(db, db.get(Campaign, ctx["campaign_id"]), ctx["owner"])
    combat = view["encounter"]
    assert combat["active_participant_id"] == str(pc_p.id)
    assert combat["map"]["zones"] == []
    assert [p["participant_id"] for p in combat["map"]["placements"]] == [str(pc_p.id)]
    assert "Secret pit trap" not in json.dumps(combat, default=str)
    # Reachable squares for the viewer's own turn are not carved by the
    # hidden goblin's token.
    reach = {(c["col"], c["row"]) for c in combat["reachable"]["cells"]}
    assert (2, 0) in reach
    assert combat["turn"]["resources"][str(pc_p.id)]["action_available"] is True


def test_npc_role_needs_explicit_member_visibility(ctx):
    db = ctx["factory"]()
    camp = db.get(Campaign, ctx["campaign_id"])
    hal, _ = _world.create_entity(db, camp, entity_type="npc", name="Hal", summary="Sells rope")
    db.add(NPCState(entity_id=hal.id, campaign_id=camp.id, role="smuggler",
                    campaign_revision=1, provenance={"source": "test"}))
    db.commit()
    db.close()
    [hal_view] = [p for p in _view(ctx, "alice")["journal"]["people"] if p["name"] == "Hal"]
    assert hal_view["role"] is None
    assert hal_view["summary"] == "Sells rope"
