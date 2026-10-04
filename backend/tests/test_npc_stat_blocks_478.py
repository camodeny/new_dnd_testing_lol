"""Issue #478 — SRD 5.2.1 stat blocks assigned to NPCs at need.

Covers the committed bestiary and its parser, the SRD encounter budget
bound, member redaction of combat stats, and the end-to-end path: the DM
assigns a block and deals damage to that NPC in one turn.
"""
import pytest

from app.dm.contract import normalize_contract
from app.rules.attacks import attacker_from_npc, defender_from_npc, hp_from_npc
from app.rules.bestiary import (
    STAT_BLOCK_SECTION,
    StatBlockError,
    check_assignable,
    encounter_xp_budget,
    get_stat_block,
    stat_block_details,
)
from app.rules.state import project_npc_details_for_viewer
from app.dm.validators import ValidatorRejectionError
from models.campaigns import Campaign
from models.world import WorldEntity
from scripts.build_bestiary import parse_monster

from tests.test_dm_mechanics_229 import _contract, _mech, _run, table  # noqa: F401  (fixture)


# ── Bestiary data ───────────────────────────────────────────────────────────


def test_committed_bestiary_matches_srd_blocks():
    bandit = get_stat_block("bandit")
    assert (bandit["challenge_rating"], bandit["xp"], bandit["armor_class"], bandit["hit_points"]) == ("1/8", 25, 12, 11)
    assert [(a["name"], a["attack_bonus"], a["damage"], a["damage_type"]) for a in bandit["attacks"]] == [
        ("Scimitar", 3, "1d6+1", "slashing"), ("Light Crossbow", 3, "1d8+1", "piercing"),
    ]
    zombie = get_stat_block("zombie")
    assert zombie["immunities"] == ["poison"]
    assert zombie["condition_immunities"] == ["exhaustion", "poisoned"]
    # Source gap fixed from the official PDF, with a cited override.
    assert get_stat_block("will-o-wisp")["abilities"]["str"] == 1
    # Unconditional riders are kept; advantage-gated ones are not.
    assert get_stat_block("adult-red-dragon")["attacks"][0]["extra_damage"] == [{"damage": "2d4", "damage_type": "fire"}]
    assert get_stat_block("goblin-warrior")["attacks"][0]["extra_damage"] == []


def test_parser_handles_alternate_xp_format_and_fails_closed():
    raw = {
        "id": "test-wyrmling", "name": "Test Wyrmling", "challenge": "3 (700 XP; PB +2)",
        "armor_class": 17, "hit_points": 60, "hit_points_roll": "8d8 + 24", "speed": "30 ft.",
        "content": "AC 17 Initiative +4 (14) HP 60 CR 3 (700 XP; PB +2) Actions Rend. Melee Attack Roll: +6, reach 5 ft. Hit: 9 (1d10 + 4) Piercing damage.",
        "tables": [{"rows": [["STR", "19", "+4", "+4", "DEX", "14", "+2", "+4", "CON", "17", "+3", "+3"],
                             ["INT", "14", "+2", "+2", "WIS", "11", "+0", "+2", "CHA", "16", "+3", "+3"]]}],
        "source": {"sectionId": "x"},
    }
    block = parse_monster(raw)
    assert (block["xp"], block["initiative_modifier"]) == (700, 4)
    assert block["attacks"] == [{"name": "Rend", "kind": "melee", "attack_bonus": 6, "damage": "1d10+4", "damage_type": "piercing", "extra_damage": []}]
    raw["tables"] = []
    with pytest.raises(ValueError, match="ability scores"):
        parse_monster(raw)


# ── Budget and details ──────────────────────────────────────────────────────


def test_budget_bounds_a_single_creature_by_party_and_difficulty():
    party = [1, 1, 1, 1]
    assert encounter_xp_budget(party, "medium") == 300
    assert encounter_xp_budget(party, "deadly") == encounter_xp_budget(party, "hard") == 400
    assert check_assignable("bandit", party_levels=party, difficulty="medium")["id"] == "bandit"
    with pytest.raises(StatBlockError) as exc:
        check_assignable("ogre", party_levels=party, difficulty="medium")  # CR 2, 450 XP
    assert exc.value.code == "stat_block_over_budget"
    assert "alternatives:" in str(exc.value)
    with pytest.raises(StatBlockError) as exc:
        check_assignable("bandt", party_levels=party, difficulty="medium")
    assert exc.value.code == "unknown_stat_block" and "bandit" in str(exc.value)


def test_assigned_details_drive_the_rules_engine_without_dm_values():
    details = stat_block_details(get_stat_block("guard"))
    assert hp_from_npc(details=details).maximum == 11
    assert defender_from_npc(details=details).armor_class == 16
    offense = attacker_from_npc(details=details)
    assert (offense.attack_name, offense.attack_bonus, offense.damage_expression) == ("Spear", 3, "1d6+1")


def test_members_never_see_combat_stats():
    details = {"description": "a tired gate guard", **stat_block_details(get_stat_block("guard"))}
    member = project_npc_details_for_viewer(details, is_authority=False)
    assert member == {"description": "a tired gate guard"}
    assert project_npc_details_for_viewer(details, is_authority=True)["armor_class"] == 16


# ── End to end ──────────────────────────────────────────────────────────────


def _npc(s, camp_id, name="Gate Thug"):
    from app.world.service import create_entity

    npc, _ = create_entity(s, s.get(Campaign, camp_id), entity_type="npc", name=name)
    s.commit()
    return npc


def _assign(npc, monster_id, effect_id="stat_1"):
    return {"id": effect_id, "effect_type": "assign_stat_block",
            "arguments": {"npc_entity_id": str(npc.id), "monster_id": monster_id}}


def test_assign_and_damage_an_npc_in_one_turn(table):
    s, camp_id, thread_id, _ = table
    npc = _npc(s, camp_id)
    raw = _contract([_mech(npc.id, target={"type": "npc", "id": str(npc.id)}, damage_dice="1d4", damage_type="fire")])
    raw["staged_effects"] = [_assign(npc, "bandit")]
    turn, attempt = _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(raw))

    assert turn.status == "succeeded"
    details = s.get(WorldEntity, npc.id).details
    assert details[STAT_BLOCK_SECTION]["monster_id"] == "bandit"
    [damage] = [e for e in attempt.staged_effects if e["effect_type"] == "apply_attack_damage"]
    assert damage["arguments"]["visibility"] == "dm_private"
    assert details["hit_points"]["current"] == 11 - damage["arguments"]["damage_total"]


def test_over_budget_block_is_refused_with_alternatives_then_retried(table):
    s, camp_id, thread_id, _ = table  # one level-3 PC at medium: 225 XP
    npc = _npc(s, camp_id, "Hulking Brute")
    feedbacks = []

    def adjudicate(packet, feedback=None):
        feedbacks.append(feedback)
        pick = "bandit" if feedback and "stat_block_over_budget" in feedback else "ogre"
        raw = _contract([], text="A hulking brute blocks the bridge.")
        raw["staged_effects"] = [_assign(npc, pick)]
        return normalize_contract(raw)

    turn, _ = _run(s, camp_id, thread_id, adjudicate)

    assert turn.status == "succeeded"
    assert "alternatives:" in feedbacks[-1]
    assert s.get(WorldEntity, npc.id).details[STAT_BLOCK_SECTION]["monster_id"] == "bandit"


def test_assigned_stats_are_canon(table):
    s, camp_id, thread_id, _ = table
    npc = _npc(s, camp_id)
    npc.details = {**(npc.details or {}), **stat_block_details(get_stat_block("guard"))}
    s.commit()
    raw = _contract([])
    raw["staged_effects"] = [_assign(npc, "bandit")]

    with pytest.raises(ValidatorRejectionError):
        _run(s, camp_id, thread_id, lambda packet, feedback=None: normalize_contract(raw))
    s.expire_all()
    assert s.get(WorldEntity, npc.id).details[STAT_BLOCK_SECTION]["monster_id"] == "guard"


def test_damage_to_unstatted_npc_tells_the_dm_how_to_fix_it(table):
    s, camp_id, thread_id, _ = table
    npc = _npc(s, camp_id)
    feedbacks = []

    def adjudicate(packet, feedback=None):
        feedbacks.append(feedback)
        mechanics = [] if feedback else [_mech(npc.id, target={"type": "npc", "id": str(npc.id)}, damage_dice="1d4", damage_type="fire")]
        return normalize_contract(_contract(mechanics))

    turn, _ = _run(s, camp_id, thread_id, adjudicate)
    assert turn.status == "succeeded"
    assert "assign_stat_block" in feedbacks[-1]
