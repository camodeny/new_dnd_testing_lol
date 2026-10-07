"""Issue #234 — code resolves PC attack → hit → damage within the turn."""
import json
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import sessionmaker

if not hasattr(SQLiteTypeCompiler, "_patched_jsonb"):
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"  # type: ignore
    SQLiteTypeCompiler._patched_jsonb = True  # type: ignore

from app.combat.attacks import AttackRollError, attack_roll_modifier, plan_attack  # noqa: E402
from app.combat.service import fulfill_human_initiative  # noqa: E402
from app.dm.contract import CONTRACT_VERSION, EntityRef, normalize_contract  # noqa: E402
from app.dm.execution import execute_dm_attempt  # noqa: E402
from app.rolls.service import RollLifecycleError, fulfill_roll  # noqa: E402
from database import Base  # noqa: E402
from models.campaigns import Campaign, CampaignMember  # noqa: E402
from models.characters import Character, Dnd5eCharacterSheet  # noqa: E402
from models.combat import EncounterParticipant  # noqa: E402
from models.dm import DmTurn, DmTurnAttempt, PlayerRollFulfillment, PlayerRollRequest  # noqa: E402
from models.profiles import Profile  # noqa: E402
from models.threads import CampaignThread  # noqa: E402
from models.world import WorldEntity  # noqa: E402
from tests.support.combat import dm_place_tokens, dm_start_encounter  # noqa: E402

WEAPONS = [
    {"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "slashing"},
    {"name": "Longbow", "attack_bonus": 4, "damage": "1d8+2", "damage_type": "piercing",
     "properties": "Ammunition (Range 150/600), Heavy, Two-Handed"},
]


@pytest.fixture
def table(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'attacks.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner, camp_id, thread_id, char_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with factory() as s:
        s.add_all([
            Profile(id=owner, email="owner@example.com"),
            Campaign(id=camp_id, owner_id=owner, name="Table", revision=0),
            CampaignThread(id=thread_id, campaign_id=camp_id, thread_type="campaign", created_by=owner),
            CampaignMember(campaign_id=camp_id, user_id=owner, role="owner", selected_character_id=char_id),
        ])
        s.flush()
        s.add_all([
            Character(id=char_id, owner_id=owner, name="Hero", system="dnd5e"),
            Dnd5eCharacterSheet(character_id=char_id, owner_id=owner, character_name="Hero", level=3,
                                dexterity=14, weapons=WEAPONS),
        ])
        beast = WorldEntity(campaign_id=camp_id, entity_type="npc", name="Reef Horror", visibility="campaign",
                            details={"armor_class": 13, "resistances": [],
                                     "hit_points": {"current": 20, "maximum": 20, "temporary": 0}})
        s.add(beast)
        s.commit()
        yield {"s": s, "owner": owner, "camp_id": camp_id, "thread_id": thread_id, "char_id": char_id,
               "beast": beast}


def _submit(t, text="I swing my sword at the reef horror."):
    from app.dm.turns import coordinate_turn
    from app.submissions.service import accept_submission

    s = t["s"]
    accept_submission(s, campaign_id=t["camp_id"], user_id=t["owner"], character_id=t["char_id"],
                      raw_content=text, segments=[{"type": "ic", "text": text}], thread_id=str(t["thread_id"]))
    s.commit()
    turn, attempt = coordinate_turn(s, t["camp_id"], str(t["thread_id"]), commit=False)
    s.commit()
    return turn, attempt


def _roll_contract(roll: dict):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "await_roll", "reason": "combat",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": "The reef horror rears up from the surf.", "claim_kind": "observation",
            "origin": "dm_adjudication", "visibility": "public",
        }]}],
        "roll_request": {"label": "Strike", "reason_public": "Roll for your strike.", **roll},
    })


def _attack(t, request_id="atk_1", **extra):
    return _roll_contract({
        "request_id": request_id, "character_id": str(t["char_id"]), "roll_kind": "attack",
        "ability_or_skill": "Longsword", "attack_name": "Longsword",
        "target_ref": {"type": "npc", "id": str(t["beast"].id)}, **extra,
    })


def _damage(t, attack_request_id="atk_1"):
    return _roll_contract({
        "request_id": f"dmg_{attack_request_id}", "character_id": str(t["char_id"]), "roll_kind": "damage",
        "ability_or_skill": "Longsword damage", "attack_request_id": attack_request_id,
    })


def _narrate(text):
    return normalize_contract({
        "contract_version": CONTRACT_VERSION, "mode": "respond", "reason": "resolve",
        "beats": [{"id": "beat_1", "type": "narration", "claims": [{
            "text": text, "claim_kind": "observation", "origin": "dm_adjudication", "visibility": "public",
        }]}],
    })


def _only_pending(s, turn_id) -> PlayerRollRequest:
    (row,) = s.execute(select(PlayerRollRequest).where(
        PlayerRollRequest.turn_id == turn_id, PlayerRollRequest.status == "pending",
    )).scalars().all()
    return row


def _fulfill(t, req, payload):
    _, fulfillment, resumed, _ = fulfill_roll(t["s"], request_id=req.id, actor_id=t["owner"],
                                              payload={"visibility": "public", **payload})
    t["s"].commit()
    return fulfillment, resumed


def _hp(t) -> int:
    t["s"].expire_all()
    return t["s"].get(WorldEntity, t["beast"].id).details["hit_points"]["current"]


def _evidence(packet, request_key):
    for lane in packet.lanes:
        for record in lane.records:
            if record.record_id == f"roll_evidence:{request_key}":
                return record
    return None


def test_hit_requests_damage_and_hp_changes_once(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    attack_req = _only_pending(s, turn.id)
    assert (attack_req.target_kind, attack_req.target_id, attack_req.attack_name) == ("npc", str(t["beast"].id), "Longsword")
    assert attack_req.dc_private is None

    fulfillment, resumed = _fulfill(t, attack_req, {"source": "app", "raw_rolls": [10], "modifier": 5, "total": 15})
    assert fulfillment.resolution["outcome"] == "hit" and fulfillment.resolution["damage_dice"] == "1d8+3"
    seen = {}

    def request_damage(packet, feedback=None):
        seen["attack"] = _evidence(packet, "atk_1")
        return _damage(t)

    execute_dm_attempt(s, resumed.id, adjudicate=request_damage, narrator="deterministic")
    outcome = seen["attack"].value["outcome"]
    assert outcome["result"] == "hit" and outcome["critical"] is False
    assert "armor_class" not in json.dumps(seen["attack"].value)
    assert seen["attack"].visibility == "dm_only"

    damage_req = _only_pending(s, turn.id)
    assert (damage_req.roll_kind, damage_req.damage_dice, damage_req.attack_request_id) == ("damage", "1d8+3", attack_req.id)
    assert _hp(t) == 20  # nothing applies until the turn commits

    _, resumed = _fulfill(t, damage_req, {"source": "app", "raw_rolls": [6], "modifier": 3, "total": 9})
    result = execute_dm_attempt(s, resumed.id, adjudicate=lambda p, f=None: _narrate("Steel bites into hide."),
                                narrator="deterministic")
    assert result.attempt.status == "succeeded"
    assert _hp(t) == 11
    staged = s.get(DmTurnAttempt, resumed.id).staged_effects
    damage_effects = [e for e in staged if e["effect_type"] == "apply_attack_damage"]
    assert len(damage_effects) == 1 and damage_effects[0]["arguments"]["damage_total"] == 9
    snapshot = json.dumps(s.get(DmTurnAttempt, resumed.id).contract_snapshot)
    assert "Reef Horror takes 9 slashing damage from Longsword." in snapshot
    assert s.get(DmTurn, turn.id).status == "succeeded"

    # Later turns never re-apply this turn's damage.
    turn2, attempt2 = _submit(t, "I catch my breath.")
    execute_dm_attempt(s, attempt2.id, adjudicate=lambda p, f=None: _narrate("The surf hisses."),
                       narrator="deterministic")
    assert _hp(t) == 11


def test_miss_refuses_damage(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    fulfillment, resumed = _fulfill(t, _only_pending(s, turn.id), {"source": "app", "raw_rolls": [3], "modifier": 5, "total": 8})
    assert fulfillment.resolution["outcome"] == "miss"
    feedback_seen = []

    def adjudicate(packet, feedback=None):
        feedback_seen.append(feedback)
        return _damage(t) if len(feedback_seen) == 1 else _narrate("The blade glances off.")

    execute_dm_attempt(s, resumed.id, adjudicate=adjudicate, narrator="deterministic")
    assert "damage_attack_missed" in (feedback_seen[1] or "")
    assert not s.execute(select(PlayerRollRequest).where(PlayerRollRequest.roll_kind == "damage")).first()
    assert _hp(t) == 20


def test_critical_doubles_dice_and_physical_totals_resolve(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    # Physical roll reported as a total: the code-owned +5 recovers the natural 20.
    fulfillment, resumed = _fulfill(t, _only_pending(s, turn.id), {"source": "physical", "total": 25})
    assert fulfillment.resolution["outcome"] == "critical"
    assert fulfillment.modifier == 5
    execute_dm_attempt(s, resumed.id, adjudicate=lambda p, f=None: _damage(t), narrator="deterministic")
    damage_req = _only_pending(s, turn.id)
    assert damage_req.damage_dice == "2d8+3"
    with pytest.raises(RollLifecycleError):
        _fulfill(t, damage_req, {"source": "physical", "total": 20})  # 17 is more than 2d8 can roll
    s.rollback()
    fulfillment, _ = _fulfill(t, damage_req, {"source": "physical", "total": 13})
    damage = fulfillment.resolution["damage"]
    assert damage["final_total"] == 13 and sorted(damage["dice_rolled"]) == [2, 8]


def test_attack_never_carries_a_model_dc(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    feedback_seen = []

    def adjudicate(packet, feedback=None):
        feedback_seen.append(feedback)
        return _attack(t, dc_private=12) if len(feedback_seen) == 1 else _attack(t, request_id="atk_2")

    execute_dm_attempt(s, attempt.id, adjudicate=adjudicate, narrator="deterministic")
    assert "attack_dc_forbidden" in (feedback_seen[1] or "")
    assert _only_pending(s, turn.id).request_key == "atk_2"


def test_app_roll_must_use_the_sheet_bonus(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    with pytest.raises(RollLifecycleError, match=r"adds \+5"):
        _fulfill(t, _only_pending(s, turn.id), {"source": "app", "raw_rolls": [10], "modifier": 9, "total": 19})


def test_target_ac_stays_out_of_player_payloads(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    req = _only_pending(s, turn.id)
    sheet = s.execute(select(Dnd5eCharacterSheet)).scalars().one()
    assert attack_roll_modifier(sheet, req) == {"modifier": 5, "label": "Longsword"}
    fulfillment, _ = _fulfill(t, req, {"source": "app", "raw_rolls": [10], "modifier": 5, "total": 15})
    public = json.dumps([req.to_dict(), fulfillment.to_dict()])
    for hidden in ("armor_class", "resolution", "target_id", "dc_private"):
        assert hidden not in public


def _encounter(t, *, pc_first=True, beast_cell=(1, 0)):
    s = t["s"]
    turn, attempt = _submit(t, "Steel rings out.")
    encounter = dm_start_encounter(
        s, t["camp_id"], turn.id, attempt.id,
        [{"character_id": str(t["char_id"])}, {"npc_entity_id": str(t["beast"].id)}],
        map={"width": 40, "height": 40}, npc_d20=1 if pc_first else 20,
    )
    pc = s.execute(select(EncounterParticipant).where(EncounterParticipant.character_id == t["char_id"])).scalars().one()
    beast = s.execute(select(EncounterParticipant).where(EncounterParticipant.npc_entity_id == t["beast"].id)).scalars().one()
    raw = 20 if pc_first else 1
    fulfill_human_initiative(s, encounter.id, pc.id, actor_id=t["owner"], payload={
        "source": "app", "raw_rolls": [raw], "modifier": pc.initiative_modifier, "total": raw + pc.initiative_modifier,
    })
    s.commit()
    dm_place_tokens(s, t["camp_id"], turn.id, attempt.id, encounter.id, [
        {"participant_id": str(pc.id), "col": 0, "row": 0},
        {"participant_id": str(beast.id), "col": beast_cell[0], "row": beast_cell[1]},
    ])
    return encounter


def _plan(t, attack_name):
    s = t["s"]
    return plan_attack(s, s.get(Campaign, t["camp_id"]), character_id=t["char_id"],
                       target_ref=EntityRef(type="npc", id=str(t["beast"].id)), attack_name=attack_name)


def test_encounter_checks_turn_reach_and_range(table):
    t = table
    _encounter(t, beast_cell=(1, 1))  # diagonal neighbour: 5 ft (Chebyshev)
    assert _plan(t, "Longsword").advantage_state == "normal"


@pytest.mark.parametrize("cell,attack_name,expected", [
    ((2, 0), "Longsword", "out_of_range"),
    ((20, 20), "Longbow", "normal"),  # 100 ft, inside 150 ft normal range
    ((35, 0), "Longbow", "disadvantage"),  # 175 ft: long range
])
def test_range_uses_grid_distance(table, cell, attack_name, expected):
    t = table
    _encounter(t, beast_cell=cell)
    if expected == "out_of_range":
        with pytest.raises(AttackRollError) as exc:
            _plan(t, attack_name)
        assert exc.value.code == "out_of_range" and "10 ft away" in str(exc.value)
    else:
        assert _plan(t, attack_name).advantage_state == expected


def test_attack_waits_for_the_attackers_turn(table):
    t = table
    _encounter(t, pc_first=False)
    with pytest.raises(AttackRollError) as exc:
        _plan(t, "Longsword")
    assert exc.value.code == "not_attackers_turn"


def test_one_damage_roll_per_hit(table):
    t, s = table, table["s"]
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    _, resumed = _fulfill(t, _only_pending(s, turn.id), {"source": "app", "raw_rolls": [12], "modifier": 5, "total": 17})
    execute_dm_attempt(s, resumed.id, adjudicate=lambda p, f=None: _damage(t), narrator="deterministic")
    damage_req = _only_pending(s, turn.id)
    _, resumed = _fulfill(t, damage_req, {"source": "app", "raw_rolls": [4], "modifier": 3, "total": 7})
    feedback_seen = []

    def adjudicate(packet, feedback=None):
        feedback_seen.append(feedback)
        if len(feedback_seen) == 1:
            contract = _damage(t)
            return contract.model_copy(update={"roll_request": contract.roll_request.model_copy(update={"request_id": "dmg_again"})})
        return _narrate("The horror reels.")

    execute_dm_attempt(s, resumed.id, adjudicate=adjudicate, narrator="deterministic")
    assert "damage_damage_already_requested" in (feedback_seen[1] or "")
    assert _hp(t) == 13
    assert s.execute(select(PlayerRollFulfillment)).scalars().all().__len__() == 2


def test_damage_respects_target_resistance(table):
    t, s = table, table["s"]
    t["beast"].details = {**t["beast"].details, "resistances": ["slashing"]}
    s.commit()
    turn, attempt = _submit(t)
    execute_dm_attempt(s, attempt.id, adjudicate=lambda p, f=None: _attack(t), narrator="deterministic")
    _, resumed = _fulfill(t, _only_pending(s, turn.id), {"source": "app", "raw_rolls": [15], "modifier": 5, "total": 20})
    execute_dm_attempt(s, resumed.id, adjudicate=lambda p, f=None: _damage(t), narrator="deterministic")
    _, resumed = _fulfill(t, _only_pending(s, turn.id), {"source": "app", "raw_rolls": [8], "modifier": 3, "total": 11})
    execute_dm_attempt(s, resumed.id, adjudicate=lambda p, f=None: _narrate("The blade skids."), narrator="deterministic")
    assert _hp(t) == 15  # 11 halved, rounded down
