"""Rules / combat domain — pure deterministic 5e mechanics.

Must not import FastAPI, database, or provider code. Operates on value
objects and returns results for the application layer to persist.
"""

from app.rules.mechanics import (  # noqa: F401
    ALL_ABILITIES,
    ALL_SKILLS,
    MECHANICS_VERSION,
    RULES_REVISION,
    SKILL_ABILITY,
    CharacterMechanics,
    MechanicsError,
    ability_modifier,
    get_character_mechanics,
    get_character_mechanics_for_sheet,
    proficiency_for_level,
    query_armor_class,
    query_attacks,
    query_conditions,
    query_hit_points,
    query_initiative,
    query_passive_perception,
    query_resources,
    query_skill_modifier,
    query_save_modifier,
    query_speed,
    query_spellcasting,
)

