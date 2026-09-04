"""Contract — #330: every editable CharacterDraft field must round-trip.

Builds a fully populated frontend-style payload (flat + nested groups, as
emitted by ``toCharacterPayload``), maps it through
``Dnd5eCharacterSheet.from_frontend`` → ``to_dict`` → ``from_frontend``
(simulated save/reload), and compares every supported field.
"""

import uuid

from models.characters import Dnd5eCharacterSheet


def _full_payload() -> dict:
    return {
        "name": "Roundtrip Hero",
        "player_name": "Test Player",
        "race": "Half-Elf",
        "subrace": "Wood Elf",
        "alignment": "Chaotic Good",
        "background": "Outlander",
        "experience_points": 6500,
        "total_level": 5,
        "ability_scores": {
            "strength": 15,
            "dexterity": 14,
            "constitution": 13,
            "intelligence": 12,
            "wisdom": 16,
            "charisma": 10,
        },
        "strength": 15,
        "dexterity": 14,
        "constitution": 13,
        "intelligence": 12,
        "wisdom": 16,
        "charisma": 10,
        "combat": {
            "max_hp": 44,
            "current_hp": 32,
            "temp_hp": 5,
            "armor_class": 16,
            "initiative_bonus": 3,
            "speed": 30,
            "death_save_successes": 1,
            "death_save_failures": 0,
        },
        "max_hp": 44,
        "current_hp": 32,
        "temp_hp": 5,
        "armor_class": 16,
        "initiative_bonus": 3,
        "speed": 30,
        "death_save_successes": 1,
        "death_save_failures": 0,
        "general": {
            "inspiration": True,
            "proficiency_bonus": 3,
            "passive_perception": 15,
            "exhaustion_level": 0,
            "encumbrance_status": "normal",
        },
        "inspiration": True,
        "proficiency_bonus": 3,
        "passive_perception": 15,
        "exhaustion_level": 0,
        "encumbrance_status": "normal",
        "spellcasting": {
            "spellcasting_ability": "wisdom",
            "spell_save_dc": 14,
            "spell_attack_bonus": 6,
            "spell_slots": {"1": {"max": 4, "used": 1}, "2": {"max": 3, "used": 0}},
        },
        "spellcasting_ability": "wisdom",
        "spell_save_dc": 14,
        "spell_attack_bonus": 6,
        "spell_slots": {"1": {"max": 4, "used": 1}, "2": {"max": 3, "used": 0}},
        "currency": {"cp": 11, "sp": 22, "ep": 33, "gp": 44, "pp": 5},
        "cp": 11,
        "sp": 22,
        "ep": 33,
        "gp": 44,
        "pp": 5,
        "personality": {
            "personality_traits": "Brave and curious",
            "ideals": "Freedom",
            "bonds": "My crew",
            "flaws": "Reckless",
        },
        "personality_traits": "Brave and curious",
        "ideals": "Freedom",
        "bonds": "My crew",
        "flaws": "Reckless",
        "appearance": {
            "age": "87",
            "height": "5'9\"",
            "weight": "160 lb",
            "eyes": "Green",
            "skin": "Tan",
            "hair": "Black",
            "character_appearance": "Scarred veteran with a cloak",
        },
        "age": "87",
        "height": "5'9\"",
        "weight": "160 lb",
        "eyes": "Green",
        "skin": "Tan",
        "hair": "Black",
        "character_appearance": "Scarred veteran with a cloak",
        "background_details": {
            "backstory": "Raised by wolves, trained by monks.",
            "allies_organizations": "The Emerald Enclave",
            "additional_features_traits": "Wanderer",
            "treasure": "A dragon hoard map",
        },
        "backstory": "Raised by wolves, trained by monks.",
        "allies_organizations": "The Emerald Enclave",
        "additional_features_traits": "Wanderer",
        "treasure": "A dragon hoard map",
        "classes": [{"class_name": "Ranger", "subclass": "Hunter", "level": 3, "hit_die_type": "d10"}],
        "skills": [{"skill_name": "Stealth", "is_proficient": True}],
        "saving_throws": [{"ability": "dexterity", "is_proficient": True}],
        "proficiencies": [{"proficiency_type": "language", "name": "Elvish"}],
        "features": [{"name": "Favored Enemy", "description": "Orcs"}],
        "weapons": [{"name": "Longbow", "attack_bonus": 6, "damage": "1d8+3"}],
        "equipment": [{"name": "Rope", "quantity": 1}],
        "spells": [{"name": "Cure Wounds", "spell_level": 1}],
        "cantrips": [{"name": "Light"}],
        "resources": [{"name": "Favored Foe", "current": 1, "max": 2}],
        "companions": [{"name": "Wolf", "companion_type": "beast"}],
        "conditions": [{"condition_name": "Blessed"}],
    }


def test_full_draft_maps_every_field():
    owner = uuid.uuid4()
    sheet = Dnd5eCharacterSheet.from_frontend(_full_payload(), owner_id=owner)
    assert sheet.character_name == "Roundtrip Hero"
    assert sheet.player_name == "Test Player"
    assert sheet.race == "Half-Elf"
    assert sheet.subrace == "Wood Elf"
    assert sheet.alignment == "Chaotic Good"
    assert sheet.background == "Outlander"
    assert sheet.experience_points == 6500
    assert sheet.level == 5
    assert (sheet.strength, sheet.dexterity, sheet.constitution) == (15, 14, 13)
    assert (sheet.intelligence, sheet.wisdom, sheet.charisma) == (12, 16, 10)
    assert (sheet.hit_points_max, sheet.hit_points_current, sheet.hit_points_temp) == (44, 32, 5)
    assert (sheet.armor_class, sheet.initiative_bonus, sheet.speed) == (16, 3, 30)
    assert (sheet.death_save_successes, sheet.death_save_failures) == (1, 0)
    assert sheet.inspiration is True
    assert sheet.proficiency_bonus == 3
    assert sheet.passive_perception == 15
    assert sheet.exhaustion_level == 0
    assert sheet.encumbrance_status == "normal"
    assert sheet.spellcasting_ability == "wisdom"
    assert sheet.spell_save_dc == 14
    assert sheet.spell_attack_bonus == 6
    assert sheet.spell_slots == {"1": {"max": 4, "used": 1}, "2": {"max": 3, "used": 0}}
    assert (sheet.cp, sheet.sp, sheet.ep, sheet.gp, sheet.pp) == (11, 22, 33, 44, 5)
    assert sheet.personality_traits == "Brave and curious"
    assert sheet.ideals == "Freedom"
    assert sheet.bonds == "My crew"
    assert sheet.flaws == "Reckless"
    assert sheet.age == "87"
    assert sheet.height == "5'9\""
    assert sheet.weight == "160 lb"
    assert sheet.eyes == "Green"
    assert sheet.skin == "Tan"
    assert sheet.hair == "Black"
    assert sheet.appearance == "Scarred veteran with a cloak"
    assert sheet.backstory == "Raised by wolves, trained by monks."
    assert sheet.allies_and_organizations == "The Emerald Enclave"
    assert sheet.features_and_traits == "Wanderer"
    assert sheet.treasure == "A dragon hoard map"
    assert sheet.classes[0]["class_name"] == "Ranger"
    assert sheet.skills[0]["skill_name"] == "Stealth"
    assert sheet.saving_throws[0]["ability"] == "dexterity"
    assert sheet.proficiencies[0]["name"] == "Elvish"
    assert sheet.features[0]["name"] == "Favored Enemy"
    assert sheet.weapons[0]["name"] == "Longbow"
    assert sheet.equipment[0]["name"] == "Rope"
    assert sheet.spells[0]["name"] == "Cure Wounds"
    assert sheet.cantrips[0]["name"] == "Light"
    assert sheet.resources[0]["name"] == "Favored Foe"
    assert sheet.companions[0]["name"] == "Wolf"
    assert sheet.conditions[0]["condition_name"] == "Blessed"


def test_read_emits_hit_points_and_frontend_aliases():
    owner = uuid.uuid4()
    sheet = Dnd5eCharacterSheet.from_frontend(_full_payload(), owner_id=owner)
    data = sheet.to_dict()
    assert data["hit_points"] == 32
    assert data["max_hp"] == 44
    assert data["current_hp"] == 32
    assert data["temp_hp"] == 5
    assert data["character_appearance"] == "Scarred veteran with a cloak"
    assert data["allies_organizations"] == "The Emerald Enclave"
    assert data["additional_features_traits"] == "Wanderer"
    assert data["currency"] == {"cp": 11, "sp": 22, "ep": 33, "gp": 44, "pp": 5}
    assert data["personality"]["flaws"] == "Reckless"
    assert data["background_details"]["treasure"] == "A dragon hoard map"
    assert data["ability_scores"]["wisdom"] == 16
    assert data["combat"]["max_hp"] == 44
    assert data["general"]["proficiency_bonus"] == 3
    assert data["spellcasting"]["spellcasting_ability"] == "wisdom"


def test_save_reload_round_trip_preserves_every_field():
    owner = uuid.uuid4()
    sheet = Dnd5eCharacterSheet.from_frontend(_full_payload(), owner_id=owner)
    # Simulated save/reload: serialize the ORM row, then re-hydrate from it.
    reloaded = Dnd5eCharacterSheet.from_frontend(sheet.to_dict(), owner_id=owner)
    first, second = sheet.to_dict(), reloaded.to_dict()
    for key in (
        "character_name", "player_name", "race", "subrace", "alignment", "background",
        "experience_points", "level",
        "strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma",
        "hit_points_max", "hit_points_current", "hit_points_temp",
        "armor_class", "initiative_bonus", "speed",
        "death_save_successes", "death_save_failures",
        "inspiration", "proficiency_bonus", "passive_perception", "exhaustion_level",
        "encumbrance_status", "spellcasting_ability", "spell_save_dc", "spell_attack_bonus",
        "spell_slots", "cp", "sp", "ep", "gp", "pp",
        "personality_traits", "ideals", "bonds", "flaws",
        "age", "height", "weight", "eyes", "skin", "hair",
        "appearance", "backstory", "allies_and_organizations", "features_and_traits", "treasure",
        "classes", "skills", "saving_throws", "proficiencies", "features", "weapons",
        "equipment", "spells", "cantrips", "resources", "companions", "conditions",
        "hit_points", "max_hp", "current_hp", "temp_hp",
        "character_appearance", "allies_organizations", "additional_features_traits",
    ):
        assert second[key] == first[key], f"round-trip mismatch on {key}"


def test_flat_only_payload_still_maps_nested_fields():
    owner = uuid.uuid4()
    sheet = Dnd5eCharacterSheet.from_frontend(
        {
            "name": "Flat Hero",
            "cp": 7,
            "personality_traits": "Bold",
            "character_appearance": "Tall",
            "allies_organizations": "Guild",
            "additional_features_traits": "Feat",
            "treasure": "Gold",
            "hit_points": {"maximum": 30, "current": 20, "temporary": 2},
        },
        owner_id=owner,
    )
    assert (sheet.cp, sheet.personality_traits, sheet.appearance) == (7, "Bold", "Tall")
    assert sheet.allies_and_organizations == "Guild"
    assert sheet.features_and_traits == "Feat"
    assert sheet.treasure == "Gold"
    assert (sheet.hit_points_max, sheet.hit_points_current, sheet.hit_points_temp) == (30, 20, 2)
