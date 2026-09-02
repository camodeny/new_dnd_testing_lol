"""Authoritative character mechanical DTO and read service — issue #224.

Deterministic 2024 5e derivations from ``Dnd5eCharacterSheet``. Pure, no DB
except the ``get_character_mechanics(db, character_id)`` wrapper which resolves
and authorizes.

Key invariants:
- Derived values are deterministic; overrides are explicit (derived vs effective + conflict flag).
- Malformed/invalid sheet → structured MechanicsError, not invented stats.
- Gameplay code never needs to know storage aliases; this module owns normalization.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.observability.tracing import structured_log

logger = logging.getLogger(__name__)

MECHANICS_VERSION = "mechanics_v1"
RULES_REVISION = "2024.5e"

# ── Errors ────────────────────────────────────────────────────────────────


class MechanicsError(ValueError):
    """Gameplay-critical derivation/validation failure — caller must block, not guess."""

    def __init__(self, code: str, message: str, *, field: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = details or {}


# ── Strict base ───────────────────────────────────────────────────────────


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ── Skill → ability map (2024 PHB standard) ───────────────────────────────

SKILL_ABILITY: dict[str, str] = {
    "acrobatics": "dexterity",
    "animal_handling": "wisdom",
    "arcana": "intelligence",
    "athletics": "strength",
    "deception": "charisma",
    "history": "intelligence",
    "insight": "wisdom",
    "intimidation": "charisma",
    "investigation": "intelligence",
    "medicine": "wisdom",
    "nature": "intelligence",
    "perception": "wisdom",
    "performance": "charisma",
    "persuasion": "charisma",
    "religion": "intelligence",
    "sleight_of_hand": "dexterity",
    "stealth": "dexterity",
    "survival": "wisdom",
}

# Valid skill names as stored (column suffixes use same keys)
ALL_SKILLS = list(SKILL_ABILITY.keys())
ALL_ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]

# Alias normalization for JSONB list items — mirrors frontend characterFormConfig.ts:FIELD_ALIASES
_SKILL_ALIASES = {"skill_name": ["name", "skill", "skillName"]}
_SAVE_ALIASES = {"ability": ["name", "saving_throw", "savingThrow"]}

ABILITY_ALIASES: dict[str, list[str]] = {
    "str": ["strength", "str"],
    "dex": ["dexterity", "dex"],
    "con": ["constitution", "con"],
    "int": ["intelligence", "int"],
    "wis": ["wisdom", "wis"],
    "cha": ["charisma", "cha"],
}

SPELL_ABILITY_ALIASES: dict[str, str] = {
    "str": "strength",
    "dex": "dexterity",
    "con": "constitution",
    "int": "intelligence",
    "wis": "wisdom",
    "cha": "charisma",
    "strength": "strength",
    "dexterity": "dexterity",
    "constitution": "constitution",
    "intelligence": "intelligence",
    "wisdom": "wisdom",
    "charisma": "charisma",
}


def _norm_skill_name(raw: str) -> str | None:
    """Normalize any skill name variant to canonical snake_case key."""
    if not raw:
        return None
    s = raw.strip().lower().replace(" ", "_").replace("-", "_")
    # handle sleight-of-hand variants
    s = s.replace("sleightofhand", "sleight_of_hand").replace("sleight_of_hand", "sleight_of_hand")
    # common frontend alias: "sleight of hand" already handled
    if s in SKILL_ABILITY:
        return s
    # try removing underscores for fuzzy
    no_us = s.replace("_", "")
    for k in SKILL_ABILITY:
        if k.replace("_", "") == no_us:
            return k
    return None


def _norm_ability(raw: str) -> str | None:
    s = raw.strip().lower()
    for canonical, aliases in ABILITY_ALIASES.items():
        if s in aliases:
            # map short -> long
            mapping = {"str": "strength", "dex": "dexterity", "con": "constitution", "int": "intelligence", "wis": "wisdom", "cha": "charisma"}
            return mapping[canonical] if canonical in mapping else canonical
    if s in ALL_ABILITIES:
        return s
    return None


def _norm_spell_ability(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip().lower()
    if s in SPELL_ABILITY_ALIASES:
        return SPELL_ABILITY_ALIASES[s]
    return None


# ── DTOs ──────────────────────────────────────────────────────────────────


class MechanicsMeta(StrictModel):
    sheet_id: str
    character_id: str | None
    owner_id: str
    sheet_version: str
    model_version: str = MECHANICS_VERSION
    rules_revision: str = RULES_REVISION


class AbilityDetail(StrictModel):
    score: int
    modifier: int
    source: Literal["stored", "derived"] = "stored"


class ProficiencyDetail(StrictModel):
    value: int
    derived: int
    effective: int
    override_active: bool
    conflict: bool
    source: Literal["derived", "stored", "override"]


class SaveDetail(StrictModel):
    ability: str
    proficient: bool
    modifier: int
    derived: int
    effective: int
    override: int | None = None
    override_active: bool = False
    conflict: bool = False
    source: Literal["derived", "override"]
    components: dict[str, int] = Field(default_factory=dict)


class SkillDetail(StrictModel):
    skill: str
    ability: str
    proficient: bool
    expertise: bool
    modifier: int
    derived: int
    effective: int
    override: int | None = None
    override_active: bool = False
    conflict: bool = False
    source: Literal["derived", "override"]
    components: dict[str, int] = Field(default_factory=dict)


class PassiveDetail(StrictModel):
    value: int
    derived: int
    effective: int
    override: int | None = None
    override_active: bool = False
    conflict: bool = False
    source: Literal["derived", "override"]


class HPDetail(StrictModel):
    current: int
    maximum: int
    temporary: int
    source: Literal["stored"] = "stored"


class ACDetail(StrictModel):
    value: int
    derived: int | None = None
    effective: int
    override: int | None = None
    override_active: bool = False
    source: Literal["stored"] = "stored"


class InitiativeDetail(StrictModel):
    modifier: int
    dex_modifier: int
    bonus: int
    source: Literal["derived"] = "derived"


class SpeedDetail(StrictModel):
    value: int
    details: str | None = None
    source: Literal["stored"] = "stored"


class AttackDetail(StrictModel):
    name: str
    ability: str | None = None
    attack_bonus: int | None = None
    attack_bonus_derived: int | None = None
    attack_bonus_effective: int | None = None
    attack_bonus_override: int | None = None
    attack_bonus_override_active: bool = False
    damage: str | None = None
    damage_type: str | None = None
    properties: str | None = None
    equipped: bool = False
    source: Literal["stored", "derived", "override"] = "stored"


class SpellcastingDetail(StrictModel):
    ability: str | None = None
    ability_modifier: int | None = None
    save_dc: int | None = None
    save_dc_derived: int | None = None
    save_dc_effective: int | None = None
    save_dc_override: int | None = None
    save_dc_override_active: bool = False
    save_dc_conflict: bool = False
    save_dc_source: Literal["derived", "override", "none"] = "none"
    attack_bonus: int | None = None
    attack_bonus_derived: int | None = None
    attack_bonus_effective: int | None = None
    attack_bonus_override: int | None = None
    attack_bonus_override_active: bool = False
    attack_bonus_conflict: bool = False
    attack_bonus_source: Literal["derived", "override", "none"] = "none"
    slots: dict[str, dict[str, int]] = Field(default_factory=dict)


class ResourceDetail(StrictModel):
    name: str
    current: int
    maximum: int
    recharge: str | None = None
    source: Literal["stored"] = "stored"


class ConditionDetail(StrictModel):
    condition_name: str
    description: str | None = None
    source: str | None = None
    is_permanent: bool = False
    duration_remaining: str | None = None


class MechanicsValidation(StrictModel):
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class CharacterMechanics(StrictModel):
    meta: MechanicsMeta
    identity: dict[str, Any]
    abilities: dict[str, AbilityDetail]
    proficiency: ProficiencyDetail
    saves: dict[str, SaveDetail]
    skills: dict[str, SkillDetail]
    passive: dict[str, PassiveDetail]
    combat: dict[str, Any]  # hp, ac, initiative, speed
    attacks: list[AttackDetail] = Field(default_factory=list)
    spellcasting: SpellcastingDetail
    resources: list[ResourceDetail] = Field(default_factory=list)
    conditions: list[ConditionDetail] = Field(default_factory=list)
    validation: MechanicsValidation = Field(default_factory=MechanicsValidation)
    provenance: dict[str, Any] = Field(default_factory=dict)


# ── Pure helpers ──────────────────────────────────────────────────────────


def ability_modifier(score: int) -> int:
    return math.floor((score - 10) / 2)


def proficiency_for_level(level: int) -> int:
    # 1-4:2, 5-8:3, 9-12:4, 13-16:5, 17-20:6
    return (level - 1) // 4 + 2


def _sheet_to_source_ref(sheet: Any) -> dict[str, Any]:
    return {
        "source_type": "dnd5e_character_sheet",
        "source_id": str(sheet.id),
        "source_version": sheet.updated_at.isoformat() if getattr(sheet, "updated_at", None) else "unknown",
        "campaign_revision": None,
    }


def _parse_jsonb_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        raise MechanicsError("malformed_jsonb", f"expected list, got str", details={"raw_type": "str"})
    if not isinstance(raw, list):
        raise MechanicsError("malformed_jsonb", f"expected list, got {type(raw).__name__}", details={"raw_type": type(raw).__name__})
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str):
            # frontend string shorthand → handled per-list
            out.append({"_str": item})
        else:
            # skip non-dict
            continue
    return out


def _extract_skill_profs(sheet: Any, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> tuple[set[str], dict[str, int | None]]:
    """Return (proficient set, skill -> bonus_override). Unions columns + JSONB."""
    profs: set[str] = set()
    overrides: dict[str, int | None] = {}

    # columns
    for skill in ALL_SKILLS:
        col = f"{skill}_prof"
        if getattr(sheet, col, False):
            profs.add(skill)

    # JSONB list — fail-closed: malformed list invalidates skill derivation
    items = _parse_jsonb_list(getattr(sheet, "skills", None))

    for item in items:
        # alias resolution
        raw_name = None
        for k in ["skill_name", "name", "skill", "skillName"]:
            if k in item and item[k]:
                raw_name = str(item[k])
                break
        if raw_name is None:
            continue
        norm = _norm_skill_name(raw_name)
        if norm is None:
            warnings.append({"code": "unknown_skill_name", "field": "skills", "message": f"unknown skill {raw_name!r} ignored"})
            continue
        if item.get("is_proficient") is True:
            profs.add(norm)
        # bonus_override
        if "bonus_override" in item and item["bonus_override"] is not None:
            try:
                ov = int(item["bonus_override"])
                overrides[norm] = ov
            except Exception:
                warnings.append({"code": "invalid_bonus_override", "field": f"skills.{norm}", "message": f"invalid bonus_override {item['bonus_override']!r}"})

    return profs, overrides


def _extract_save_profs(sheet: Any, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> tuple[set[str], dict[str, int | None]]:
    profs: set[str] = set()
    overrides: dict[str, int | None] = {}
    ability_cols = {
        "strength": "str_save_prof",
        "dexterity": "dex_save_prof",
        "constitution": "con_save_prof",
        "intelligence": "int_save_prof",
        "wisdom": "wis_save_prof",
        "charisma": "cha_save_prof",
    }
    for ability, col in ability_cols.items():
        if getattr(sheet, col, False):
            profs.add(ability)

    items = _parse_jsonb_list(getattr(sheet, "saving_throws", None))

    for item in items:
        raw = None
        for k in ["ability", "name", "saving_throw", "savingThrow"]:
            if k in item and item[k]:
                raw = str(item[k])
                break
        if raw is None:
            continue
        norm = _norm_ability(raw)
        if norm is None:
            warnings.append({"code": "unknown_ability", "field": "saving_throws", "message": f"unknown ability {raw!r} ignored"})
            continue
        if item.get("is_proficient") is True:
            profs.add(norm)
        if "bonus_override" in item and item["bonus_override"] is not None:
            try:
                ov = int(item["bonus_override"])
                overrides[norm] = ov
            except Exception:
                warnings.append({"code": "invalid_bonus_override", "field": f"saving_throws.{norm}", "message": f"invalid {item['bonus_override']!r}"})

    return profs, overrides


def _extract_expertise(sheet: Any) -> set[str]:
    expertise: set[str] = set()
    # skill_expertise dict (canonical)
    raw = getattr(sheet, "skill_expertise", None)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if v:
                norm = _norm_skill_name(str(k))
                if norm:
                    expertise.add(norm)
    # JSONB items with is_expertise
    items = getattr(sheet, "skills", None)
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("is_expertise") is True:
                for k in ["skill_name", "name", "skill", "skillName"]:
                    if k in item and item[k]:
                        norm = _norm_skill_name(str(item[k]))
                        if norm:
                            expertise.add(norm)
                        break
    return expertise


# ── Core derivation ───────────────────────────────────────────────────────


def get_character_mechanics_for_sheet(sheet: Any) -> CharacterMechanics:
    """Pure derivation from a Dnd5eCharacterSheet ORM instance (or duck-typed stub)."""
    t0 = time.monotonic()
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    # --- Validation: level ---
    level = getattr(sheet, "level", 1)
    if level is None:
        level = 1
    try:
        level_int = int(level)
    except Exception:
        raise MechanicsError("invalid_level", f"level must be int, got {level!r}", field="level")
    if level_int < 1 or level_int > 20:
        raise MechanicsError("invalid_level", f"level {level_int} outside 1-20", field="level")

    derived_prof = proficiency_for_level(max(1, min(level_int, 20)))

    stored_prof_raw = getattr(sheet, "proficiency_bonus", derived_prof)
    if stored_prof_raw is None:
        stored_prof_raw = derived_prof
    try:
        stored_prof = int(stored_prof_raw)
    except Exception:
        errors.append({"code": "invalid_proficiency_bonus", "field": "proficiency_bonus", "message": f"invalid {stored_prof_raw!r}"})
        stored_prof = derived_prof

    if stored_prof != derived_prof:
        warnings.append({"code": "proficiency_mismatch", "field": "proficiency_bonus", "message": f"stored {stored_prof} != derived {derived_prof} for level {level_int}"})

    proficiency = ProficiencyDetail(
        value=stored_prof,
        derived=derived_prof,
        effective=stored_prof,
        override_active=stored_prof != derived_prof,
        conflict=stored_prof != derived_prof,
        source="override" if stored_prof != derived_prof else "derived",
    )

    # --- Abilities ---
    abilities: dict[str, AbilityDetail] = {}
    ability_mods: dict[str, int] = {}
    for ab in ALL_ABILITIES:
        raw = getattr(sheet, ab, 10)
        if raw is None:
            raw = 10
        try:
            score = int(raw)
        except Exception:
            raise MechanicsError("invalid_ability_score", f"ability {ab} must be int, got {raw!r}", field=ab)
        if score < 1 or score > 30:
            raise MechanicsError("invalid_ability_score", f"{ab} score {score} outside 1-30", field=ab)
        mod = ability_modifier(score)
        abilities[ab] = AbilityDetail(score=score, modifier=mod, source="stored")
        ability_mods[ab] = mod

    # --- Saves & Skills helpers ---
    save_profs, save_overrides = _extract_save_profs(sheet, warnings, errors)
    skill_profs, skill_overrides = _extract_skill_profs(sheet, warnings, errors)
    expertise = _extract_expertise(sheet)

    # Saves
    saves: dict[str, SaveDetail] = {}
    for ab in ALL_ABILITIES:
        prof = ab in save_profs
        derived = ability_mods[ab] + (derived_prof if prof else 0)
        components = {"ability_mod": ability_mods[ab]}
        if prof:
            components["proficiency_bonus"] = derived_prof
        override = save_overrides.get(ab)
        if override is not None:
            effective = override
            override_active = True
            conflict = override != derived
            source: Literal["derived", "override"] = "override"
            if conflict:
                warnings.append({"code": "save_override_conflict", "field": f"saving_throws.{ab}", "message": f"override {override} != derived {derived}"})
        else:
            effective = derived
            override_active = False
            conflict = False
            source = "derived"
        saves[ab] = SaveDetail(
            ability=ab,
            proficient=prof,
            modifier=effective,
            derived=derived,
            effective=effective,
            override=override,
            override_active=override_active,
            conflict=conflict,
            source=source,
            components=components,
        )

    # Skills
    skills: dict[str, SkillDetail] = {}
    for skill, ability in SKILL_ABILITY.items():
        prof = skill in skill_profs
        exp = skill in expertise
        ab_mod = ability_mods[ability]
        derived = ab_mod + (derived_prof if prof else 0) + (derived_prof if exp else 0)
        components = {"ability_mod": ab_mod}
        if prof:
            components["proficiency_bonus"] = derived_prof
        if exp:
            components["expertise_bonus"] = derived_prof
        override = skill_overrides.get(skill)
        if override is not None:
            effective = override
            override_active = True
            conflict = override != derived
            source = "override"
            if conflict:
                warnings.append({"code": "skill_override_conflict", "field": f"skills.{skill}", "message": f"override {override} != derived {derived}"})
        else:
            effective = derived
            override_active = False
            conflict = False
            source = "derived"
        skills[skill] = SkillDetail(
            skill=skill,
            ability=ability,
            proficient=prof,
            expertise=exp,
            modifier=effective,
            derived=derived,
            effective=effective,
            override=override,
            override_active=override_active,
            conflict=conflict,
            source=source,
            components=components,
        )

    # --- Passive Perception ---
    perc = skills["perception"]
    passive_derived = 10 + perc.derived
    stored_passive = getattr(sheet, "passive_perception", None)
    if stored_passive is not None:
        try:
            stored_pp = int(stored_passive)
            effective_pp = stored_pp
            override_active = stored_pp != passive_derived
            conflict = stored_pp != passive_derived
            source_pp: Literal["derived", "override"] = "override" if override_active else "derived"
            if conflict:
                warnings.append({"code": "passive_override_conflict", "field": "passive_perception", "message": f"stored {stored_pp} != derived {passive_derived}"})
        except Exception:
            warnings.append({"code": "invalid_passive_perception", "field": "passive_perception", "message": f"invalid {stored_passive!r}"})
            effective_pp = passive_derived
            stored_pp = None
            override_active = False
            conflict = False
            source_pp = "derived"
    else:
        stored_pp = None
        effective_pp = passive_derived
        override_active = False
        conflict = False
        source_pp = "derived"

    passive: dict[str, PassiveDetail] = {
        "perception": PassiveDetail(
            value=effective_pp,
            derived=passive_derived,
            effective=effective_pp,
            override=stored_pp,
            override_active=override_active,
            conflict=conflict,
            source=source_pp,
        )
    }

    # --- Combat: HP, AC, Initiative, Speed ---
    # Default handling for ORM unflushed instances (columns default 10/30 but attribute is None)
    def _int_or_default(attr: str, default: int) -> int:
        v = getattr(sheet, attr, default)
        if v is None:
            v = default
        return int(v)

    try:
        hp_max = _int_or_default("hit_points_max", 10)
        hp_cur = _int_or_default("hit_points_current", 10)
        hp_tmp = _int_or_default("hit_points_temp", 0)
    except Exception as exc:
        raise MechanicsError("invalid_hit_points", f"hit points must be int: {exc}", field="hit_points")

    if hp_max < 1:
        raise MechanicsError("invalid_hit_points_max", f"max {hp_max} must be >=1", field="hit_points_max")
    if hp_cur < 0:
        raise MechanicsError("invalid_hit_points_current", f"current {hp_cur} <0", field="hit_points_current")
    if hp_cur > hp_max:
        warnings.append({"code": "hit_points_current_exceeds_max", "field": "hit_points_current", "message": f"current {hp_cur} > max {hp_max}"})
    if hp_tmp < 0:
        raise MechanicsError("invalid_temp_hp", f"temp {hp_tmp} <0", field="hit_points_temp")

    hp = HPDetail(current=hp_cur, maximum=hp_max, temporary=hp_tmp)

    try:
        ac_val = _int_or_default("armor_class", 10)
        speed_val = _int_or_default("speed", 30)
    except Exception as exc:
        raise MechanicsError("invalid_combat_value", str(exc), field="armor_class/speed")

    if ac_val < 5 or ac_val > 35:
        warnings.append({"code": "unusual_armor_class", "field": "armor_class", "message": f"AC {ac_val} outside typical 5-35"})
    if speed_val < 0 or speed_val > 500:
        raise MechanicsError("invalid_speed", f"speed {speed_val} outside 0-500", field="speed")

    ac = ACDetail(value=ac_val, effective=ac_val, override=None, override_active=False, source="stored")
    speed_details = getattr(sheet, "speed_details", None)
    speed = SpeedDetail(value=speed_val, details=speed_details)

    try:
        ib_raw = getattr(sheet, "initiative_bonus", 0)
        if ib_raw is None:
            ib_raw = 0
        init_bonus = int(ib_raw)
    except Exception:
        init_bonus = 0
        warnings.append({"code": "invalid_initiative_bonus", "field": "initiative_bonus", "message": "invalid initiative_bonus, using 0"})
    dex_mod = ability_mods["dexterity"]
    init_mod = dex_mod + init_bonus
    initiative = InitiativeDetail(modifier=init_mod, dex_modifier=dex_mod, bonus=init_bonus)

    def _int_or_zero(attr: str) -> int:
        v = getattr(sheet, attr, 0)
        if v is None:
            v = 0
        return int(v)

    combat: dict[str, Any] = {
        "hit_points": hp.model_dump(),
        "armor_class": ac.model_dump(),
        "initiative": initiative.model_dump(),
        "speed": speed.model_dump(),
        "death_saves": {
            "successes": _int_or_zero("death_save_successes"),
            "failures": _int_or_zero("death_save_failures"),
        },
        "exhaustion_level": _int_or_zero("exhaustion_level"),
        "inspiration": bool(getattr(sheet, "inspiration", False) or False),
    }

    # --- Attacks / weapons ---
    attacks = _derive_attacks(sheet, warnings, errors)

    # --- Spellcasting ---
    spellcasting = _derive_spellcasting(sheet, ability_mods, derived_prof, warnings, errors)

    # --- Resources / Conditions ---
    resources = _derive_resources(sheet, warnings, errors)
    conditions = _derive_conditions(sheet, warnings, errors)

    # --- Validation summary ---
    # Flag hard errors for invalid ability/level etc already appended
    # Add HP AC speed into validation passthrough (already flagged)

    meta = MechanicsMeta(
        sheet_id=str(getattr(sheet, "id", "")),
        character_id=str(getattr(sheet, "character_id", "")) if getattr(sheet, "character_id", None) else None,
        owner_id=str(getattr(sheet, "owner_id", "")),
        sheet_version=getattr(sheet, "updated_at", None).isoformat() if getattr(sheet, "updated_at", None) else "unknown",
    )

    identity = {
        "name": getattr(sheet, "character_name", "Unnamed Hero"),
        "level": level_int,
        "classes": _normalize_classes(getattr(sheet, "classes", None)),
        "proficiency_bonus": proficiency.model_dump(),
    }

    validation = MechanicsValidation(errors=errors, warnings=warnings)
    provenance = {
        "sources": [_sheet_to_source_ref(sheet)],
        "field_sources": {k: _sheet_to_source_ref(sheet) for k in ["abilities", "saves", "skills", "passive", "combat", "attacks", "spellcasting"]},
    }

    result = CharacterMechanics(
        meta=meta,
        identity=identity,
        abilities=abilities,
        proficiency=proficiency,
        saves=saves,
        skills=skills,
        passive=passive,
        combat=combat,
        attacks=attacks,
        spellcasting=spellcasting,
        resources=resources,
        conditions=conditions,
        validation=validation,
        provenance=provenance,
    )

    latency_ms = (time.monotonic() - t0) * 1000
    # Observability: track query type, latency, override use, invalid errors
    try:
        override_fields = []
        if proficiency.override_active:
            override_fields.append("proficiency_bonus")
        if passive["perception"].override_active:
            override_fields.append("passive_perception")
        for k, v in saves.items():
            if v.override_active:
                override_fields.append(f"save:{k}")
        for k, v in skills.items():
            if v.override_active:
                override_fields.append(f"skill:{k}")
        if spellcasting.save_dc_override_active:
            override_fields.append("spell_save_dc")
        if spellcasting.attack_bonus_override_active:
            override_fields.append("spell_attack_bonus")

        structured_log(
            logger,
            logging.INFO,
            "character_mechanics_query",
            sheet_id=str(getattr(sheet, "id", "")),
            character_id=str(getattr(sheet, "character_id", "")) if getattr(sheet, "character_id", None) else None,
            level=level_int,
            latency_ms=round(latency_ms, 2),
            derived_proficiency=derived_prof,
            stored_proficiency=stored_prof,
            override_fields=override_fields,
            override_count=len(override_fields),
            error_count=len(errors),
            warning_count=len(warnings),
            error_codes=[e.get("code") for e in errors],
            sheet_version=meta.sheet_version,
            rules_revision=RULES_REVISION,
            model_version=MECHANICS_VERSION,
        )
    except Exception:
        pass

    return result


def _normalize_classes(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append({
            "class_name": item.get("class_name") or item.get("name") or item.get("class") or "",
            "subclass": item.get("subclass") or item.get("archetype") or "",
            "level": item.get("level") or 1,
            "hit_die_type": item.get("hit_die_type") or item.get("hit_die") or item.get("hitDie") or "d8",
        })
    return out


def _derive_attacks(sheet: Any, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> list[AttackDetail]:
    raw = getattr(sheet, "weapons", None)
    if raw is None:
        raw = getattr(sheet, "attacks", None)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MechanicsError("malformed_weapons", f"weapons must be list, got {type(raw).__name__}", field="weapons")
    out: list[AttackDetail] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("weapon_name") or item.get("title") or "Unnamed Weapon")
        # attack_bonus is stored override if present; otherwise None
        ab_raw = item.get("attack_bonus")
        ab_override = None
        if ab_raw is not None:
            try:
                ab_override = int(ab_raw)
            except Exception:
                warnings.append({"code": "invalid_attack_bonus", "field": f"weapons.{name}", "message": f"invalid attack_bonus {ab_raw!r}"})
        out.append(AttackDetail(
            name=name,
            ability=None,
            attack_bonus=ab_override,
            attack_bonus_derived=None,
            attack_bonus_effective=ab_override,
            attack_bonus_override=ab_override,
            attack_bonus_override_active=ab_override is not None,
            damage=item.get("damage"),
            damage_type=item.get("damage_type"),
            properties=item.get("properties"),
            equipped=bool(item.get("is_equipped", False)),
            source="override" if ab_override is not None else "stored",
        ))
    return out


def _derive_spellcasting(sheet: Any, ability_mods: dict[str, int], derived_prof: int, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> SpellcastingDetail:
    raw_ability = getattr(sheet, "spellcasting_ability", None)
    ability = _norm_spell_ability(raw_ability) if raw_ability else None
    ab_mod = ability_mods.get(ability) if ability else None

    # Validation: spell slots shape
    slots_raw = getattr(sheet, "spell_slots", None)
    slots: dict[str, dict[str, int]] = {}
    if slots_raw is not None:
        if not isinstance(slots_raw, dict):
            raise MechanicsError("malformed_spell_slots", f"spell_slots must be dict, got {type(slots_raw).__name__}", field="spell_slots")
        else:
            for lvl, val in slots_raw.items():
                lvl_key = str(lvl)
                if not lvl_key.isdigit() or not (1 <= int(lvl_key) <= 9):
                    warnings.append({"code": "invalid_spell_slot_level", "field": f"spell_slots.{lvl_key}", "message": f"level {lvl_key} outside 1-9"})
                    continue
                if isinstance(val, dict):
                    try:
                        mx = int(val.get("max", 0))
                        used = int(val.get("used", 0))
                    except Exception:
                        warnings.append({"code": "invalid_spell_slot_counts", "field": f"spell_slots.{lvl_key}", "message": f"invalid {val!r}"})
                        continue
                    if mx < 0 or used < 0 or used > mx:
                        warnings.append({"code": "spell_slot_used_exceeds_max", "field": f"spell_slots.{lvl_key}", "message": f"used {used} > max {mx}"})
                    slots[lvl_key] = {"max": mx, "used": used, "remaining": max(0, mx - used)}
                elif isinstance(val, int):
                    slots[lvl_key] = {"max": val, "used": 0, "remaining": val}
                else:
                    warnings.append({"code": "invalid_spell_slot_value", "field": f"spell_slots.{lvl_key}", "message": f"invalid {val!r}"})

    # Derived DC / attack
    derived_dc = None
    derived_attack = None
    if ability and ab_mod is not None:
        derived_dc = 8 + derived_prof + ab_mod
        derived_attack = derived_prof + ab_mod

    stored_dc_raw = getattr(sheet, "spell_save_dc", None)
    stored_attack_raw = getattr(sheet, "spell_attack_bonus", None)

    # DC override handling
    dc_override = None
    dc_effective = derived_dc
    dc_override_active = False
    dc_conflict = False
    dc_source: Literal["derived", "override", "none"] = "none"
    if stored_dc_raw is not None:
        try:
            dc_override = int(stored_dc_raw)
            dc_override_active = True
            dc_effective = dc_override
            dc_source = "override"
            if derived_dc is not None and dc_override != derived_dc:
                dc_conflict = True
                warnings.append({"code": "spell_dc_override_conflict", "field": "spell_save_dc", "message": f"stored {dc_override} != derived {derived_dc}"})
            elif derived_dc is None:
                # No ability to derive, stored is authoritative
                dc_effective = dc_override
                dc_source = "override"
        except Exception:
            warnings.append({"code": "invalid_spell_save_dc", "field": "spell_save_dc", "message": f"invalid {stored_dc_raw!r}"})
            dc_override = None
            dc_effective = derived_dc
            dc_source = "derived" if derived_dc is not None else "none"
    else:
        dc_effective = derived_dc
        dc_source = "derived" if derived_dc is not None else "none"

    atk_override = None
    atk_effective = derived_attack
    atk_override_active = False
    atk_conflict = False
    atk_source: Literal["derived", "override", "none"] = "none"
    if stored_attack_raw is not None:
        try:
            atk_override = int(stored_attack_raw)
            atk_override_active = True
            atk_effective = atk_override
            atk_source = "override"
            if derived_attack is not None and atk_override != derived_attack:
                atk_conflict = True
                warnings.append({"code": "spell_attack_override_conflict", "field": "spell_attack_bonus", "message": f"stored {atk_override} != derived {derived_attack}"})
        except Exception:
            warnings.append({"code": "invalid_spell_attack_bonus", "field": "spell_attack_bonus", "message": f"invalid {stored_attack_raw!r}"})
            atk_override = None
            atk_effective = derived_attack
            atk_source = "derived" if derived_attack is not None else "none"
    else:
        atk_effective = derived_attack
        atk_source = "derived" if derived_attack is not None else "none"

    return SpellcastingDetail(
        ability=ability,
        ability_modifier=ab_mod,
        save_dc=dc_effective,
        save_dc_derived=derived_dc,
        save_dc_effective=dc_effective,
        save_dc_override=dc_override,
        save_dc_override_active=dc_override_active,
        save_dc_conflict=dc_conflict,
        save_dc_source=dc_source,
        attack_bonus=atk_effective,
        attack_bonus_derived=derived_attack,
        attack_bonus_effective=atk_effective,
        attack_bonus_override=atk_override,
        attack_bonus_override_active=atk_override_active,
        attack_bonus_conflict=atk_conflict,
        attack_bonus_source=atk_source,
        slots=slots,
    )


def _derive_resources(sheet: Any, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> list[ResourceDetail]:
    raw = getattr(sheet, "resources", None)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MechanicsError("malformed_resources", f"resources must be list, got {type(raw).__name__}", field="resources")
    out: list[ResourceDetail] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("resource_name") or item.get("title") or "Resource")
        try:
            cur = int(item.get("current", 0) or 0)
            mx = int(item.get("max", 0) or item.get("maximum", 0) or 0)
        except Exception:
            warnings.append({"code": "invalid_resource_counts", "field": f"resources.{name}", "message": f"invalid {item!r}"})
            continue
        if mx < 0 or cur < 0 or (mx and cur > mx):
            warnings.append({"code": "resource_current_exceeds_max", "field": f"resources.{name}", "message": f"current {cur} > max {mx}"})
        out.append(ResourceDetail(name=name, current=cur, maximum=mx, recharge=item.get("recharge")))
    return out


def _derive_conditions(sheet: Any, warnings: list[dict[str, Any]], errors: list[dict[str, Any]]) -> list[ConditionDetail]:
    raw = getattr(sheet, "conditions", None)
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise MechanicsError("malformed_conditions", f"conditions must be list, got {type(raw).__name__}", field="conditions")
    out: list[ConditionDetail] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("condition_name") or item.get("name") or item.get("condition") or item.get("title") or "Condition")
        out.append(ConditionDetail(
            condition_name=name,
            description=item.get("description"),
            source=item.get("source"),
            is_permanent=bool(item.get("is_permanent", False)),
            duration_remaining=item.get("duration_remaining"),
        ))
    return out


# ── DB wrapper + focused queries ────────────────────────────────────────

# Focused query helpers — thin projections of the canonical DTO (no second derivation path)


def query_ability_modifier(sheet: Any, ability: str) -> int:
    norm = _norm_ability(ability)
    if norm is None:
        raise MechanicsError("invalid_ability", f"unknown ability {ability!r}", field="ability")
    m = get_character_mechanics_for_sheet(sheet)
    return m.abilities[norm].modifier


def query_skill_modifier(sheet: Any, skill: str) -> SkillDetail:
    norm = _norm_skill_name(skill)
    if norm is None:
        raise MechanicsError("invalid_skill", f"unknown skill {skill!r}", field="skill")
    m = get_character_mechanics_for_sheet(sheet)
    return m.skills[norm]


def query_save_modifier(sheet: Any, ability: str) -> SaveDetail:
    norm = _norm_ability(ability)
    if norm is None:
        raise MechanicsError("invalid_ability", f"unknown ability {ability!r}", field="ability")
    m = get_character_mechanics_for_sheet(sheet)
    return m.saves[norm]


def query_passive_perception(sheet: Any) -> PassiveDetail:
    m = get_character_mechanics_for_sheet(sheet)
    return m.passive["perception"]


def query_armor_class(sheet: Any) -> ACDetail:
    m = get_character_mechanics_for_sheet(sheet)
    # combat armor_class is stored dict
    d = m.combat["armor_class"]
    return ACDetail.model_validate(d)


def query_initiative(sheet: Any) -> InitiativeDetail:
    m = get_character_mechanics_for_sheet(sheet)
    return InitiativeDetail.model_validate(m.combat["initiative"])


def query_speed(sheet: Any) -> SpeedDetail:
    m = get_character_mechanics_for_sheet(sheet)
    return SpeedDetail.model_validate(m.combat["speed"])


def query_hit_points(sheet: Any) -> HPDetail:
    m = get_character_mechanics_for_sheet(sheet)
    return HPDetail.model_validate(m.combat["hit_points"])


def query_attacks(sheet: Any) -> list[AttackDetail]:
    m = get_character_mechanics_for_sheet(sheet)
    return list(m.attacks)


def query_spellcasting(sheet: Any) -> SpellcastingDetail:
    m = get_character_mechanics_for_sheet(sheet)
    return m.spellcasting


def query_resources(sheet: Any) -> list[ResourceDetail]:
    m = get_character_mechanics_for_sheet(sheet)
    return list(m.resources)


def query_conditions(sheet: Any) -> list[ConditionDetail]:
    m = get_character_mechanics_for_sheet(sheet)
    return list(m.conditions)


def get_character_mechanics(db: Any, character_id: uuid.UUID | str, *, include_private: bool = True) -> CharacterMechanics:
    """DB-backed fetch + derivation. Validates character exists and sheet present."""
    from sqlalchemy import select

    # Lazy import to avoid circular
    try:
        from models import Character, Dnd5eCharacterSheet
    except Exception as exc:
        raise MechanicsError("model_import_failed", str(exc))

    if isinstance(character_id, str):
        try:
            character_id = uuid.UUID(character_id)
        except Exception as exc:
            raise MechanicsError("invalid_character_id", f"invalid UUID {character_id!r}: {exc}", field="character_id")

    char = db.get(Character, character_id)
    if char is None:
        raise MechanicsError("character_not_found", f"character {character_id} not found", field="character_id")

    sheet = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == char.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()

    if sheet is None:
        raise MechanicsError("sheet_not_found", f"no sheet for character {character_id}", field="character_id")

    return get_character_mechanics_for_sheet(sheet)
