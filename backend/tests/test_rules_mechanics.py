"""Unit fixtures for #224 — authoritative character mechanical queries."""

import uuid
import pytest
from app.rules.mechanics import (
    MechanicsError,
    ability_modifier,
    get_character_mechanics_for_sheet,
    proficiency_for_level,
    query_armor_class,
    query_attacks,
    query_hit_points,
    query_initiative,
    query_passive_perception,
    query_skill_modifier,
    query_save_modifier,
    query_spellcasting,
    query_speed,
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
        # skill profs
        skills = ["acrobatics","animal_handling","arcana","athletics","deception","history","insight","intimidation","investigation","medicine","nature","perception","performance","persuasion","religion","sleight_of_hand","stealth","survival"]
        for s in skills:
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
        self.hit_dice = kwargs.pop("hit_dice", None)
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
        # consume extras
        for k, v in kwargs.items():
            setattr(self, k, v)
        import datetime
        self.updated_at = kwargs.get("updated_at") or datetime.datetime.now(datetime.timezone.utc)
        self.created_at = self.updated_at


def test_proficiency_by_level():
    assert proficiency_for_level(1) == 2
    assert proficiency_for_level(4) == 2
    assert proficiency_for_level(5) == 3
    assert proficiency_for_level(8) == 3
    assert proficiency_for_level(9) == 4
    assert proficiency_for_level(13) == 5
    assert proficiency_for_level(17) == 6
    assert proficiency_for_level(20) == 6


def test_ability_modifiers():
    assert ability_modifier(10) == 0
    assert ability_modifier(12) == 1
    assert ability_modifier(8) == -1
    assert ability_modifier(1) == -5
    assert ability_modifier(30) == 10
    assert ability_modifier(14) == 2
    assert ability_modifier(15) == 2
    assert ability_modifier(16) == 3


def test_level_class_scores_proficiency():
    sheet = FakeSheet(level=5, strength=16, dexterity=14, proficiency_bonus=3)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.proficiency.derived == 3
    assert m.proficiency.value == 3
    assert m.abilities["strength"].modifier == 3
    assert m.abilities["dexterity"].modifier == 2
    assert m.meta.model_version == "mechanics_v1"
    assert m.identity["level"] == 5


def test_proficiency_override_detected():
    sheet = FakeSheet(level=5, proficiency_bonus=2)  # derived is 3, stored 2
    m = get_character_mechanics_for_sheet(sheet)
    assert m.proficiency.derived == 3
    assert m.proficiency.value == 2
    assert m.proficiency.override_active is True
    assert m.proficiency.conflict is True
    assert any(w["code"] == "proficiency_mismatch" for w in m.validation.warnings)


def test_skills_saves_passive():
    # wis 14 (+2), prof 2, perception proficient, stealth not, expertise on perception via dict
    sheet = FakeSheet(
        level=1, wisdom=14, dexterity=12, proficiency_bonus=2,
        perception_prof=True, stealth_prof=False,
        skill_expertise={"perception": True},
    )
    m = get_character_mechanics_for_sheet(sheet)
    # perception: 2 (wis) +2 prof +2 expertise =6
    perc = m.skills["perception"]
    assert perc.proficient is True
    assert perc.expertise is True
    assert perc.derived == 6
    assert perc.modifier == 6
    # stealth: dex 12 (+1), not proficient
    stealth = m.skills["stealth"]
    assert stealth.proficient is False
    assert stealth.derived == 1
    # passive perception =10+6=16
    assert m.passive["perception"].derived == 16
    assert m.passive["perception"].value == 16
    # save: wis not proficient (default), str not
    wis_save = m.saves["wisdom"]
    assert wis_save.proficient is False
    assert wis_save.derived == 2  # wis mod

    # add save proficiency via boolean column
    sheet2 = FakeSheet(level=1, wisdom=14, proficiency_bonus=2, wis_save_prof=True)
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.saves["wisdom"].proficient is True
    assert m2.saves["wisdom"].derived == 4  # 2 +2


def test_skill_expertise_via_jsonb_list():
    sheet = FakeSheet(
        level=3, wisdom=12, intelligence=14, proficiency_bonus=2,
        skills=[
            {"skill_name": "Perception", "is_proficient": True, "is_expertise": True},
            {"skill_name": "Arcana", "is_proficient": True},
        ],
    )
    m = get_character_mechanics_for_sheet(sheet)
    # perception: wis 12 (+1) +2 +2 expertise =5
    assert m.skills["perception"].derived == 5
    assert m.skills["perception"].expertise is True
    # arcana: int 14 (+2) +2 =4
    assert m.skills["arcana"].derived == 4
    # skill override via bonus_override
    sheet2 = FakeSheet(
        level=3, wisdom=12, proficiency_bonus=2,
        skills=[{"skill_name": "perception", "is_proficient": True, "bonus_override": 9}],
    )
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.skills["perception"].override == 9
    assert m2.skills["perception"].override_active is True
    assert m2.skills["perception"].effective == 9
    assert m2.skills["perception"].conflict is True  # derived 3 vs 9


def test_save_prof_via_jsonb_list_alias():
    sheet = FakeSheet(
        level=5, strength=16, proficiency_bonus=3,
        saving_throws=[{"ability": "strength", "is_proficient": True}],
    )
    m = get_character_mechanics_for_sheet(sheet)
    # str 16 (+3) +3 prof =6
    assert m.saves["strength"].derived == 6
    assert m.saves["strength"].proficient is True


def test_passive_perception_override():
    # wis 10 (0), prof2, perception proficient => derived 12
    sheet = FakeSheet(level=1, wisdom=10, proficiency_bonus=2, perception_prof=True, passive_perception=99)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.passive["perception"].derived == 12
    assert m.passive["perception"].override == 99
    assert m.passive["perception"].effective == 99
    assert m.passive["perception"].override_active is True
    assert m.passive["perception"].conflict is True


def test_ac_initiative_speed_hp():
    sheet = FakeSheet(
        level=1, dexterity=16, armor_class=15, initiative_bonus=1, speed=30,
        hit_points_max=12, hit_points_current=8, hit_points_temp=5,
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert m.combat["armor_class"]["value"] == 15
    assert m.combat["speed"]["value"] == 30
    assert m.combat["hit_points"]["maximum"] == 12
    assert m.combat["hit_points"]["current"] == 8
    assert m.combat["hit_points"]["temporary"] == 5
    # initiative: dex 16 (+3) +1 bonus =4
    assert m.combat["initiative"]["modifier"] == 4
    assert m.combat["initiative"]["dex_modifier"] == 3
    assert query_initiative(sheet).modifier == 4
    assert query_armor_class(sheet).value == 15
    assert query_speed(sheet).value == 30
    hp = query_hit_points(sheet)
    assert hp.maximum == 12 and hp.current == 8


def test_weapon_attack_data_alias():
    sheet = FakeSheet(
        weapons=[{"name": "Longsword", "attack_bonus": 5, "damage": "1d8+3", "damage_type": "slashing", "is_equipped": True}]
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert len(m.attacks) == 1
    assert m.attacks[0].name == "Longsword"
    assert m.attacks[0].attack_bonus == 5
    assert m.attacks[0].equipped is True

    # attacks alias (legacy)
    sheet2 = FakeSheet(attacks=[{"name": "Dagger", "attack_bonus": 4, "damage": "1d4+2"}])
    # FakeSheet sets weapons and attacks separately; to test alias, mimic ORM where weapons=None and attacks set
    sheet2.weapons = None
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert len(m2.attacks) == 1
    assert m2.attacks[0].name == "Dagger"

    assert len(query_attacks(sheet)) == 1


def test_spell_dc_attack():
    # int 16 (+3), level 5 => prof 3 => dc 14, attack 6
    sheet = FakeSheet(level=5, intelligence=16, proficiency_bonus=3, spellcasting_ability="Intelligence")
    m = get_character_mechanics_for_sheet(sheet)
    assert m.spellcasting.save_dc_derived == 14
    assert m.spellcasting.attack_bonus_derived == 6
    assert m.spellcasting.save_dc == 14
    sc = query_spellcasting(sheet)
    assert sc.save_dc == 14

    # override
    sheet2 = FakeSheet(level=5, intelligence=16, proficiency_bonus=3, spellcasting_ability="int", spell_save_dc=15, spell_attack_bonus=7)
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert m2.spellcasting.save_dc_override == 15
    assert m2.spellcasting.save_dc_effective == 15
    assert m2.spellcasting.save_dc_conflict is True
    assert m2.spellcasting.attack_bonus_conflict is True


def test_spell_slots_validation():
    sheet = FakeSheet(spell_slots={"1": {"max": 4, "used": 2}, "9": {"max": 1, "used": 1}})
    m = get_character_mechanics_for_sheet(sheet)
    assert m.spellcasting.slots["1"]["remaining"] == 2
    assert m.spellcasting.slots["9"]["remaining"] == 0

    # used > max → warning
    sheet2 = FakeSheet(spell_slots={"1": {"max": 2, "used": 5}})
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert any(w["code"] == "spell_slot_used_exceeds_max" for w in m2.validation.warnings)


def test_resources_conditions():
    sheet = FakeSheet(
        resources=[{"name": "Ki", "current": 2, "max": 3, "recharge": "short rest"}],
        conditions=[{"condition_name": "Poisoned", "description": "disadvantage"}],
    )
    m = get_character_mechanics_for_sheet(sheet)
    assert m.resources[0].name == "Ki"
    assert m.resources[0].current == 2
    assert m.conditions[0].condition_name == "Poisoned"


def test_malformed_and_invalid_sheet():
    # invalid ability score outside 1-30 collects error but still derives
    sheet = FakeSheet(strength=31, level=1)
    m = get_character_mechanics_for_sheet(sheet)
    assert any(e["code"] == "invalid_ability_score" for e in m.validation.errors)

    # level 0 invalid
    sheet2 = FakeSheet(level=0)
    m2 = get_character_mechanics_for_sheet(sheet2)
    assert any(e["code"] == "invalid_level" for e in m2.validation.errors)

    # malformed skills not list
    sheet3 = FakeSheet(skills="notalist")  # type: ignore
    m3 = get_character_mechanics_for_sheet(sheet3)
    assert any(e["field"] == "skills" for e in m3.validation.errors)

    # malformed spell_slots not dict
    sheet4 = FakeSheet(spell_slots="bad")  # type: ignore
    m4 = get_character_mechanics_for_sheet(sheet4)
    assert any(e["code"] == "malformed_spell_slots" for e in m4.validation.errors)

    # invalid skill name in list is ignored with warning, not error
    sheet5 = FakeSheet(skills=[{"skill_name": "not_a_skill", "is_proficient": True}])
    m5 = get_character_mechanics_for_sheet(sheet5)
    assert any(w["code"] == "unknown_skill_name" for w in m5.validation.warnings)


def test_creator_round_trip_still_functional():
    """Existing CRUD round-trip: frontend payload → from_frontend → mechanics."""
    import uuid as uuid_lib
    from models import Dnd5eCharacterSheet
    owner = uuid_lib.uuid4()
    payload = {
        "name": "Roundtrip Hero",
        "total_level": 3,
        "strength": 15,
        "dexterity": 14,
        "wisdom": 13,
        "armor_class": 14,
        "speed": 30,
        "max_hp": 20,
        "current_hp": 18,
        "temp_hp": 0,
        "proficiency_bonus": 2,
        "spellcasting_ability": "wisdom",
        "weapons": [{"name": "Mace", "attack_bonus": 4, "damage": "1d6+2"}],
        "resources": [{"name": "Rage", "current": 1, "max": 2}],
        "conditions": [{"condition_name": "Blessed"}],
    }
    sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=owner)
    m = get_character_mechanics_for_sheet(sheet)
    assert m.identity["name"] == "Roundtrip Hero"
    assert m.identity["level"] == 3
    assert m.abilities["strength"].modifier == 2
    assert m.combat["armor_class"]["value"] == 14
    assert m.attacks[0].name == "Mace"
    # spell dc: wis 13 (+1) + prof 2 => 11
    assert m.spellcasting.save_dc_derived == 11


def test_focused_queries_without_alias_knowledge():
    sheet = FakeSheet(level=1, wisdom=14, perception_prof=True, proficiency_bonus=2)
    # Callers don't need to know column aliases
    skill = query_skill_modifier(sheet, "perception")
    assert skill.modifier == 4  # wis +2 prof
    save = query_save_modifier(sheet, "wisdom")
    assert save.modifier == 2
    pp = query_passive_perception(sheet)
    assert pp.value == 14  # 10+4


def test_unknown_skill_ability_raises_explicit_error():
    sheet = FakeSheet()
    with pytest.raises(MechanicsError) as exc:
        query_skill_modifier(sheet, "not_a_skill")
    assert exc.value.code == "invalid_skill"
    with pytest.raises(MechanicsError):
        query_save_modifier(sheet, "luck")


def test_source_version_metadata_present():
    sheet = FakeSheet(level=2)
    m = get_character_mechanics_for_sheet(sheet)
    assert "sources" in m.provenance
    assert m.provenance["sources"][0]["source_type"] == "dnd5e_character_sheet"
    assert m.meta.model_version == "mechanics_v1"
    assert m.meta.rules_revision == "2024.5e"
    assert m.meta.sheet_version != "unknown"
