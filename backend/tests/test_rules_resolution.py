"""Unit fixtures for #225 — deterministic checks, saves, advantage, dice primitives."""

import datetime
import random
import uuid

import pytest

from app.rules.resolution import (
    ResolutionError,
    ResolutionLedger,
    combine_advantage,
    resolve_ability_check,
    resolve_d20_roll,
    resolve_modifier,
    resolve_saving_throw,
    runtime_d20,
    select_d20,
)


class FakeSheet:
    def __init__(self, **kwargs):
        self.id = kwargs.pop("id", uuid.uuid4())
        self.character_id = kwargs.pop("character_id", uuid.uuid4())
        self.owner_id = kwargs.pop("owner_id", uuid.uuid4())
        self.character_name = kwargs.pop("character_name", "Test Hero")
        self.level = kwargs.pop("level", 1)
        self.strength = kwargs.pop("strength", 10)
        self.dexterity = kwargs.pop("dexterity", 10)
        self.constitution = kwargs.pop("constitution", 10)
        self.intelligence = kwargs.pop("intelligence", 10)
        self.wisdom = kwargs.pop("wisdom", 10)
        self.charisma = kwargs.pop("charisma", 10)
        self.proficiency_bonus = kwargs.pop("proficiency_bonus", 2)
        for ab in ["str", "dex", "con", "int", "wis", "cha"]:
            setattr(self, f"{ab}_save_prof", kwargs.pop(f"{ab}_save_prof", False))
        for s in ["acrobatics", "animal_handling", "arcana", "athletics", "deception",
                  "history", "insight", "intimidation", "investigation", "medicine",
                  "nature", "perception", "performance", "persuasion", "religion",
                  "sleight_of_hand", "stealth", "survival"]:
            setattr(self, f"{s}_prof", kwargs.pop(f"{s}_prof", False))
        self.skill_expertise = kwargs.pop("skill_expertise", None)
        self.skills = kwargs.pop("skills", None)
        self.saving_throws = kwargs.pop("saving_throws", None)
        self.passive_perception = kwargs.pop("passive_perception", None)
        self.armor_class = kwargs.pop("armor_class", 10)
        self.initiative_bonus = kwargs.pop("initiative_bonus", 0)
        self.speed = kwargs.pop("speed", 30)
        self.speed_details = kwargs.pop("speed_details", None)
        self.hit_points_max = kwargs.pop("hit_points_max", 10)
        self.hit_points_current = kwargs.pop("hit_points_current", 10)
        self.hit_points_temp = kwargs.pop("hit_points_temp", 0)
        self.death_save_successes = kwargs.pop("death_save_successes", 0)
        self.death_save_failures = kwargs.pop("death_save_failures", 0)
        self.exhaustion_level = kwargs.pop("exhaustion_level", 0)
        self.inspiration = kwargs.pop("inspiration", False)
        self.weapons = kwargs.pop("weapons", None)
        self.attacks = kwargs.pop("attacks", None)
        self.spellcasting_ability = kwargs.pop("spellcasting_ability", None)
        self.spell_save_dc = kwargs.pop("spell_save_dc", None)
        self.spell_attack_bonus = kwargs.pop("spell_attack_bonus", None)
        self.spell_slots = kwargs.pop("spell_slots", None)
        self.resources = kwargs.pop("resources", None)
        self.conditions = kwargs.pop("conditions", None)
        self.classes = kwargs.pop("classes", None)
        for k, v in kwargs.items():
            setattr(self, k, v)
        now = datetime.datetime.now(datetime.timezone.utc)
        self.updated_at = now
        self.created_at = now


def hero(**kwargs):
    kwargs.setdefault("level", 3)
    kwargs.setdefault("proficiency_bonus", 2)
    return FakeSheet(**kwargs)


# ── Modifier derivation ───────────────────────────────────────────────────


def test_proficient_skill_check_is_deterministic():
    sheet = hero(wisdom=14, perception_prof=True)  # wis +2, prof +2 => +4
    first = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[14], dc=15, roll_id="r1")
    second = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[14], dc=15, roll_id="r1")
    assert first.modifier == 4
    assert first.total == 18
    assert first.success is True
    assert first.status == "resolved"
    assert first.calculation_path == "skill_authoritative"
    assert first.model_dump() == second.model_dump()  # pure retry is identical


def test_unproficient_skill_check():
    sheet = hero(dexterity=12)  # +1, stealth not proficient
    res = resolve_ability_check(roller="pc", skill="stealth", sheet=sheet, dice=[10], dc=15, roll_id="r2")
    assert res.modifier == 1
    assert res.total == 11
    assert res.success is False


def test_ability_check_without_skill():
    sheet = hero(strength=16)  # +3
    res = resolve_ability_check(roller="pc", ability="strength", sheet=sheet, dice=[9], dc=12, roll_id="r3")
    assert res.modifier == 3
    assert res.total == 12
    assert res.success is True
    assert res.calculation_path == "ability_authoritative"


def test_save_proficient_and_unproficient():
    sheet = hero(wisdom=14, wis_save_prof=True, proficiency_bonus=2)
    res = resolve_saving_throw(roller="pc", ability="wisdom", sheet=sheet, dice=[8], dc=12, roll_id="s1")
    assert res.modifier == 4  # +2 wis +2 prof
    assert res.total == 12
    assert res.success is True
    assert res.calculation_path == "save_authoritative"

    res2 = resolve_saving_throw(roller="pc", ability="strength", sheet=sheet, dice=[8], dc=12, roll_id="s2")
    assert res2.modifier == 0  # str 10, not proficient
    assert res2.success is False


def test_expertise_hook_and_situational_bonus():
    sheet = hero(wisdom=14, perception_prof=True, skill_expertise={"perception": True})
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[10],
                                dc=20, situational_bonus=2, roll_id="e1")
    # +2 wis +2 prof +2 expertise +2 situational = +8; 10+8=18 < 20
    assert res.modifier == 8
    assert res.total == 18
    assert res.success is False
    assert res.modifier_components["expertise_bonus"] == 2
    assert res.modifier_components["situational_bonus"] == 2

    # Custom class-feature hook (e.g. future bardic/flash-of-genius style bonus)
    def guidance(ctx):
        return 3 if ctx.kind == "check" else 0

    res2 = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[10],
                                 dc=20, hooks=[guidance], roll_id="e2")
    assert res2.modifier == 9  # 6 base + 3 hook
    assert "hook:guidance" in res2.modifier_components
    assert res2.provenance["hook_names"] == ["guidance"]


# ── Advantage / disadvantage ──────────────────────────────────────────────


def test_advantage_keeps_higher():
    sheet = hero(wisdom=10)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                dice=[7, 18], advantage_state="advantage", dc=15, roll_id="a1")
    assert res.die_kept == 18
    assert res.dice_dropped == [7]
    assert res.total == 18
    assert res.success is True


def test_disadvantage_keeps_lower():
    sheet = hero(wisdom=10)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                dice=[7, 18], advantage_state="disadvantage", dc=8, roll_id="d1")
    assert res.die_kept == 7
    assert res.dice_dropped == [18]
    assert res.total == 7
    assert res.success is False


def test_select_d20_doubles_and_counts():
    kept, dropped = select_d20([12, 12], "advantage")
    assert kept == 12 and dropped == [12]
    with pytest.raises(ResolutionError) as exc:
        select_d20([5], "advantage")
    assert exc.value.code == "invalid_dice_count"
    with pytest.raises(ResolutionError) as exc:
        select_d20([0], "normal")
    assert exc.value.code == "invalid_die_value"
    with pytest.raises(ResolutionError) as exc:
        select_d20([21], "normal")
    assert exc.value.code == "invalid_die_value"


def test_advantage_cancellation_rules():
    assert combine_advantage([]) == "normal"
    assert combine_advantage(["advantage", "advantage"]) == "advantage"  # never stacks
    assert combine_advantage(["disadvantage", "disadvantage"]) == "disadvantage"
    assert combine_advantage(["advantage", "disadvantage"]) == "normal"  # cancels
    assert combine_advantage(["advantage", "advantage", "disadvantage"]) == "normal"

    sheet = hero(wisdom=10)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[16],
                                advantage_sources=["advantage", "disadvantage"], dc=16, roll_id="c1")
    assert res.advantage_state == "normal"
    assert res.total == 16
    assert res.success is True


def test_natural_20_and_1_override_total_vs_dc():
    sheet = hero(wisdom=10)
    crit = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                 dice=[20], dc=100, roll_id="n20")
    assert crit.is_natural_20 is True
    assert crit.success is True  # 2024 D20 Test auto-success despite impossible DC

    fumble = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                   dice=[1], dc=1, roll_id="n1")
    assert fumble.is_natural_1 is True
    assert fumble.success is False  # auto-fail despite trivial DC


# ── Dice provenance: player vs runtime ────────────────────────────────────


def test_pc_dice_are_supplied_never_generated():
    sheet = hero(wisdom=10)
    with pytest.raises(ResolutionError) as exc:
        resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=None, dc=10)
    assert exc.value.code == "missing_player_die"
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet, dice=[11], dc=10, roll_id="p1")
    assert res.die_source == "player_supplied"


def test_npc_runtime_generated_and_dm_supplied():
    seeded = random.Random(1234)
    res = resolve_saving_throw(roller="npc", ability="dexterity", modifier=2,
                               dice=None, dc=12, rng=seeded, roll_id="npc1")
    assert res.die_source == "runtime_generated"
    assert 1 <= res.die_kept <= 20
    assert res.total == res.die_kept + 2
    # Seeded runtime dice are reproducible
    res_again = resolve_saving_throw(roller="npc", ability="dexterity", modifier=2,
                                     dice=None, dc=12, rng=random.Random(1234), roll_id="npc1")
    assert res_again.die_kept == res.die_kept

    supplied = resolve_saving_throw(roller="npc", ability="dexterity", modifier=2,
                                    dice=[17], dc=12, roll_id="npc2")
    assert supplied.die_source == "dm_supplied"
    assert supplied.total == 19
    assert supplied.success is True


def test_runtime_d20_validation():
    assert len(runtime_d20(1, rng=random.Random(0))) == 1
    assert len(runtime_d20(2, rng=random.Random(0))) == 2
    with pytest.raises(ResolutionError):
        runtime_d20(3, rng=random.Random(0))


# ── Hidden DC privacy ─────────────────────────────────────────────────────


def test_hidden_dc_absent_from_public_projection():
    sheet = hero(wisdom=14, perception_prof=True)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                dice=[14], dc=17, dc_visibility="hidden", roll_id="h1")
    assert res.dc == 17
    public = res.public_projection()
    assert "dc" not in public
    assert public["success"] is True  # observable outcome stays
    assert public["total"] == 18  # PC dice are public
    full = res.to_event_payload(include_private=True)
    assert full["dc"] == 17
    assert full["dice_all"] == [14]


def test_public_dc_included_when_explicitly_public():
    sheet = hero(wisdom=10)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                dice=[10], dc=12, dc_visibility="public", roll_id="h2")
    assert res.public_projection()["dc"] == 12


def test_hidden_npc_roll_hides_dice_and_total():
    res = resolve_saving_throw(roller="npc", ability="dexterity", modifier=2,
                               dice=[17], dc=12, die_visibility="hidden", roll_id="h3")
    public = res.public_projection()
    assert "die_kept" not in public
    assert "total" not in public
    assert "dice_all" not in public
    assert public["success"] is True
    assert "dc" not in public


# ── Missing inputs → explicit error ───────────────────────────────────────


def test_missing_inputs_produce_explicit_errors():
    with pytest.raises(ResolutionError) as exc:
        resolve_ability_check(roller="npc", skill="perception", sheet=None, modifier=None, dice=[10])
    assert exc.value.code == "missing_mechanical_input"

    sheet = hero()
    with pytest.raises(ResolutionError) as exc:
        resolve_ability_check(roller="pc", skill="not_a_skill", sheet=sheet, dice=[10])
    assert exc.value.code == "unknown_skill"

    with pytest.raises(ResolutionError) as exc:
        resolve_saving_throw(roller="pc", ability="luck", sheet=sheet, dice=[10])
    assert exc.value.code == "unknown_ability"

    with pytest.raises(ResolutionError) as exc:
        resolve_ability_check(roller="pc", sheet=sheet, dice=[10])  # neither skill nor ability
    assert exc.value.code == "missing_target"

    with pytest.raises(ResolutionError) as exc:
        resolve_ability_check(roller="pc", skill="perception", sheet=FakeSheet(strength=99), dice=[10])
    assert exc.value.code == "invalid_ability_score"  # invalid sheet blocks, never guesses


def test_modifier_resolution_requires_exactly_one_source():
    sheet = hero()
    with pytest.raises(ResolutionError) as exc:
        resolve_modifier(sheet, kind="check", skill="perception", modifier=3)
    assert exc.value.code == "conflicting_modifier_inputs"
    resolved = resolve_modifier(None, kind="save", ability="dexterity", modifier=-1)
    assert resolved.modifier == -1
    assert resolved.calculation_path == "dm_supplied"


# ── Duplicate application guard ───────────────────────────────────────────


def test_duplicate_resolution_cannot_apply_twice():
    ledger = ResolutionLedger()
    assert ledger.apply("roll-abc") == "applied"
    assert ledger.apply("roll-abc") == "duplicate"
    assert ledger.is_duplicate("roll-abc") is True
    assert ledger.is_duplicate("roll-other") is False
    assert ledger.apply("roll-other") == "applied"
    assert len(ledger) == 2


def test_result_carries_audit_provenance():
    sheet = hero(wisdom=14, perception_prof=True)
    res = resolve_ability_check(roller="pc", skill="perception", sheet=sheet,
                                dice=[14], dc=15, roll_id="audit1")
    assert res.provenance["calculation_path"] == "skill_authoritative"
    assert res.provenance["mechanics_version"] == "mechanics_v1"
    assert res.provenance["rules_revision"] == "2024.5e"
    assert res.provenance["die_source"] == "player_supplied"
    assert res.provenance["resolution_version"] == "resolution_v1"
    assert res.modifier_components  # input values recorded for audit/explanation
