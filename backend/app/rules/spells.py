"""Core spellcasting validation and mechanical effect primitives — issue #228.

Deterministic 2024 5e spell eligibility, resource staging, and common launch
effect shapes over authoritative character state (#224) and rules data
(#223). The AI DM decides *whether* a spell is cast; this module validates
legality, stages resource use, and resolves the mechanical patterns without
asking any model to do bookkeeping or arithmetic.

Storage authority (pre-alpha, single canonical impl):
- PCs: ``Dnd5eCharacterSheet`` — ``spells`` / ``cantrips`` JSONB (known /
  prepared lists), ``spell_slots`` JSONB, ``spellcasting_ability`` /
  ``spell_save_dc`` / ``spell_attack_bonus`` columns, ``extras["concentration"]``.
- NPCs/monsters: ``WorldEntity.details`` — ``spells`` / ``cantrips`` /
  ``spell_slots`` / ``concentration`` keys. Hidden (``dm_only``) NPC spell
  lists stay DM-only in projections while still driving mechanics.

Invariants (mirroring #225/#226/#227):
- Every function is pure calculation over supplied state; persistence happens
  in the staged-effect handlers (``app.dm.effects``) inside the turn-commit
  transaction, so multi-effect mutation is all-or-nothing.
- Every staged cast carries a caller-supplied stable ``cast_id`` (never
  minted here) — the in-commit and cross-retry dedup identity. Staged effect
  and mutation IDs derive deterministically from it, so a duplicate retry
  stages byte-identical effects and replays instead of double-spending.
- Invalid uses raise :class:`SpellError` before any staging, narration, or
  commit. Nothing is consumed before a valid resolution exists.
- Decision models only ever choose among code-supplied candidates; this
  module performs no model calls.
- Unsupported long-tail spells never fake deterministic support: validation
  returns a structured fallback descriptor pointing at the DM + rules
  retrieval path.

Out of scope: VTT area geometry/target selection (#181), exhaustive
spell-by-spell scripting, homebrew spells.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.observability.tracing import structured_log
from app.rules.mechanics import (
    MECHANICS_VERSION,
    RULES_REVISION,
    MechanicsError,
    get_character_mechanics_for_sheet,
)

logger = logging.getLogger(__name__)

SPELLS_VERSION = "spells_v1"


# ── Errors ────────────────────────────────────────────────────────────────


class SpellError(ValueError):
    """Explicit invalid spell use — caller must surface, not guess."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.field = field
        self.details = details or {}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


# ── Supported-spell catalog (common launch patterns) ──────────────────────
#
# Prioritizes common spells / mechanical patterns, not exhaustive scripting.
# Each entry carries its authoritative rule/source ref for dispute and
# explanation paths. Citations preserve CC BY attribution for SRD 5.2.1
# content (see app/rules/metadata.py:ATTRIBUTION); PHB 2024 refs name the
# print source for spells outside the SRD excerpt.

CastingTime = Literal["action", "bonus_action", "reaction"]
CasterKind = Literal["pc", "npc"]
SpellPattern = Literal[
    "attack_roll",
    "saving_throw",
    "damage",
    "healing",
    "condition",
    "concentration",
    "buff",
    "area",
]


class SpellDef(StrictModel):
    """Static mechanical shape of one supported common spell."""

    key: str
    name: str
    level: int  # 0 = cantrip
    school: str
    casting_time: CastingTime
    range_ft: int | None = None
    range_text: str | None = None
    area: str | None = None  # simple metadata only — no VTT geometry (#181)
    concentration: bool = False
    duration: str | None = None
    attack_roll: bool = False
    save_ability: str | None = None
    damage_base: str | None = None  # strict NdM[+-K], cantrips scale by level
    damage_upcast: str | None = None  # extra dice per slot above base level
    damage_type: str | None = None
    healing_base: str | None = None
    healing_upcast: str | None = None
    condition: str | None = None  # applied on a failed save (save spells)
    patterns: list[SpellPattern] = Field(default_factory=list)
    rule_id: str = ""
    source: str = ""  # e.g. "SRD 5.2.1" / "PHB 2024"
    citation: str = ""


def _spell(
    key: str,
    name: str,
    level: int,
    school: str,
    casting_time: CastingTime,
    source: str,
    patterns: list[SpellPattern],
    **kwargs: Any,
) -> SpellDef:
    rule_id = f"srd521.spells.{key.replace('_', '-')}"
    citation = f"{source}, Spells: {name}"
    if "attack_roll" in patterns:
        kwargs.setdefault("attack_roll", True)
    return SpellDef(
        key=key,
        name=name,
        level=level,
        school=school,
        casting_time=casting_time,
        patterns=patterns,
        rule_id=rule_id,
        source=source,
        citation=citation,
        **kwargs,
    )


SUPPORTED_SPELLS: dict[str, SpellDef] = {
    d.key: d
    for d in (
        _spell(
            "fire_bolt", "Fire Bolt", 0, "evocation", "action", "SRD 5.2.1",
            ["attack_roll", "damage"],
            range_ft=120, damage_base="1d10", damage_type="fire",
            duration="instantaneous",
        ),
        _spell(
            "eldritch_blast", "Eldritch Blast", 0, "evocation", "action", "PHB 2024",
            ["attack_roll", "damage"],
            range_ft=120, damage_base="1d10", damage_type="force",
            duration="instantaneous",
        ),
        _spell(
            "sacred_flame", "Sacred Flame", 0, "evocation", "action", "SRD 5.2.1",
            ["saving_throw", "damage"],
            range_ft=60, save_ability="dexterity",
            damage_base="1d8", damage_type="radiant",
            duration="instantaneous",
        ),
        _spell(
            "magic_missile", "Magic Missile", 1, "evocation", "action", "SRD 5.2.1",
            ["damage"],
            range_ft=120, damage_base="3d4+3", damage_upcast="1d4+1",
            damage_type="force", duration="instantaneous",
        ),
        _spell(
            "cure_wounds", "Cure Wounds", 1, "abjuration", "action", "SRD 5.2.1",
            ["healing"],
            range_text="touch", healing_base="2d8", healing_upcast="2d8",
            duration="instantaneous",
        ),
        _spell(
            "healing_word", "Healing Word", 1, "abjuration", "bonus_action", "SRD 5.2.1",
            ["healing"],
            range_ft=60, healing_base="2d4", healing_upcast="2d4",
            duration="instantaneous",
        ),
        _spell(
            "guiding_bolt", "Guiding Bolt", 1, "evocation", "action", "SRD 5.2.1",
            ["attack_roll", "damage"],
            range_ft=120, damage_base="4d6", damage_upcast="1d6",
            damage_type="radiant", duration="1 round",
        ),
        _spell(
            "fireball", "Fireball", 3, "evocation", "action", "SRD 5.2.1",
            ["saving_throw", "damage", "area"],
            range_ft=150, area="20-foot-radius sphere", save_ability="dexterity",
            damage_base="8d6", damage_upcast="1d6", damage_type="fire",
            duration="instantaneous",
        ),
        _spell(
            "bless", "Bless", 1, "enchantment", "action", "SRD 5.2.1",
            ["concentration", "buff"],
            range_ft=30, concentration=True, duration="1 minute (concentration)",
        ),
        _spell(
            "hold_person", "Hold Person", 2, "enchantment", "action", "SRD 5.2.1",
            ["saving_throw", "condition", "concentration"],
            range_ft=60, concentration=True, save_ability="wisdom",
            condition="paralyzed", duration="1 minute (concentration)",
        ),
        _spell(
            "shield", "Shield", 1, "abjuration", "reaction", "SRD 5.2.1",
            ["buff"],
            range_text="self", duration="1 round",
        ),
        _spell(
            "hex", "Hex", 1, "enchantment", "bonus_action", "SRD 5.2.1",
            ["concentration", "damage"],
            range_ft=90, concentration=True, damage_base="1d6",
            damage_type="necrotic", duration="1 hour (concentration)",
        ),
    )
}

SUPPORTED_SPELL_NAMES: tuple[str, ...] = tuple(d.name for d in SUPPORTED_SPELLS.values())


def _observe(code: str, **fields: Any) -> None:
    try:
        structured_log(logger, logging.INFO, "spellcasting_validation", invalid_input=code, **fields)
    except Exception:
        pass


def normalize_spell_name(raw: Any) -> str:
    """Normalize a spell name to its catalog key; raises SpellError when blank."""
    if not isinstance(raw, str) or not raw.strip():
        raise SpellError("missing_spell_name", "a spell name is required", field="spell")
    key = raw.strip().lower().replace("-", "_").replace("  ", " ")
    key = "_".join(key.split())
    # collapse punctuation variants ("fire-bolt" already handled; "firebolt" fuzzy)
    nosym = re.sub(r"[^a-z0-9_]", "", key)
    for candidate in SUPPORTED_SPELLS:
        if candidate == key or candidate.replace("_", "") == nosym:
            return candidate
    return key  # unsupported long-tail key — callers fall back, never guess


def is_spell_supported(name: Any) -> bool:
    try:
        return normalize_spell_name(name) in SUPPORTED_SPELLS
    except SpellError:
        return False


def get_spell_def(name: Any) -> SpellDef:
    """Return the catalog entry, or raise ``unsupported_spell`` for the long tail."""
    key = normalize_spell_name(name)
    try:
        return SUPPORTED_SPELLS[key]
    except KeyError:
        raise SpellError(
            "unsupported_spell",
            f"spell {str(name)!r} has no deterministic launch support: use the DM + rules-retrieval fallback",
            field="spell",
            details={"spell_key": key, "supported": sorted(SUPPORTED_SPELLS)},
        ) from None


def spell_rule_ref(name: Any) -> dict[str, str]:
    """Authoritative rule/source ref for dispute/explanation paths.

    Supported spells return their catalog citation; the long tail returns a
    retrieval pointer (corpus + query) so callers can explain *why* a cast
    fell back instead of inventing mechanics.
    """
    try:
        definition = get_spell_def(name)
    except SpellError:
        key = normalize_spell_name(name) if isinstance(name, str) and name.strip() else "unknown"
        return {
            "rule_id": f"srd521.spells.{key.replace('_', '-')}",
            "source": "SRD 5.2.1",
            "citation": f"SRD 5.2.1 rules retrieval: query spells/{key}",
            "retrieval_query": f"2024 spell {name}",
            "supported": "false",
        }
    return {
        "rule_id": definition.rule_id,
        "source": definition.source,
        "citation": definition.citation,
        "supported": "true",
    }


# ── Known / prepared spell queries ────────────────────────────────────────


class KnownSpells(StrictModel):
    """Normalized spell lists from existing sheet / NPC details data."""

    cantrips: list[str] = Field(default_factory=list)
    spells: list[str] = Field(default_factory=list)
    prepared: list[str] = Field(default_factory=list)
    lists_present: bool = False  # False when the store carries no spell lists at all
    provenance: dict[str, Any] = Field(default_factory=dict)


def _normalize_spell_entries(raw: Any) -> tuple[list[str], list[str]]:
    """Parse a JSONB spell list of strings or ``{name, prepared}`` dicts.

    Returns ``(names, prepared_names)``. String shorthand and dicts without
    an explicit opt-out count as castable; dicts with ``prepared: false`` or
    the frontend character-editor ``is_prepared: false`` (stored unchanged by
    ``Dnd5eCharacterSheet.from_frontend``) are known but not prepared.
    Unparseable entries are skipped (fail-closed per entry, not per list).
    """
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        raise SpellError(
            "malformed_spell_list",
            f"spell list must be a list, got {type(raw).__name__}",
            field="spells",
        )
    names: list[str] = []
    prepared: list[str] = []
    for item in raw:
        entry_name: str | None = None
        is_prepared = True
        if isinstance(item, str):
            entry_name = item
        elif isinstance(item, dict):
            for key in ("name", "spell_name", "spell", "title"):
                if isinstance(item.get(key), str) and item[key].strip():
                    entry_name = str(item[key])
                    break
            if entry_name is None:
                continue
            if item.get("prepared") is False or item.get("is_prepared") is False:
                is_prepared = False
        else:
            continue
        norm = entry_name.strip().lower()
        if norm and norm not in names:
            names.append(norm)
        if norm and is_prepared and norm not in prepared:
            prepared.append(norm)
    return names, prepared


def query_known_spells(sheet: Any) -> KnownSpells:
    """Normalize PC known/prepared spells + cantrips from sheet JSONB."""
    cantrips_raw = getattr(sheet, "cantrips", None)
    spells_raw = getattr(sheet, "spells", None)
    lists_present = cantrips_raw is not None or spells_raw is not None
    cantrips, cantrips_prepared = _normalize_spell_entries(cantrips_raw)
    spells, spells_prepared = _normalize_spell_entries(spells_raw)
    return KnownSpells(
        cantrips=cantrips,
        spells=spells,
        prepared=list(dict.fromkeys(cantrips_prepared + spells_prepared)),
        lists_present=lists_present,
        provenance={"holder": "pc", "sheet_id": str(getattr(sheet, "id", ""))},
    )


def query_npc_spells(details: dict[str, Any] | None) -> KnownSpells:
    """Normalize hidden NPC spell lists from ``WorldEntity.details``.

    Mechanically usable via the returned value object; the stored lists
    themselves stay DM-private and must go through
    :func:`project_npc_spells_for_viewer` before leaving the authority lane.
    """
    store = details if isinstance(details, dict) else {}
    cantrips_raw = store.get("cantrips")
    spells_raw = store.get("spells")
    lists_present = cantrips_raw is not None or spells_raw is not None
    cantrips, cantrips_prepared = _normalize_spell_entries(cantrips_raw)
    spells, spells_prepared = _normalize_spell_entries(spells_raw)
    return KnownSpells(
        cantrips=cantrips,
        spells=spells,
        prepared=list(dict.fromkeys(cantrips_prepared + spells_prepared)),
        lists_present=lists_present,
        provenance={"holder": "npc", "visibility": "dm_private"},
    )


def project_npc_spells_for_viewer(details: dict[str, Any] | None, is_authority: bool) -> dict[str, Any]:
    """Authority-safe copy of NPC details with hidden spell lists redacted.

    Authority sees stored lists; ordinary viewers see no spell names — only
    the fact that projection occurred. Mechanics always run on the stored
    shape, never on this projection.
    """
    if not isinstance(details, dict):
        return {}
    if is_authority:
        return {k: v for k, v in details.items()}
    projected = {k: v for k, v in details.items()}
    if "spells" in projected:
        projected["spells"] = []
    if "cantrips" in projected:
        projected["cantrips"] = []
    if "spell_slots" in projected:
        projected["spell_slots"] = {}
    return projected


# ── Slot / resource queries ───────────────────────────────────────────────


def query_spell_slots(store: Any) -> dict[str, dict[str, int]]:
    """Remaining spell slots from a sheet or NPC details dict.

    Returns ``{level: {"max": N, "used": M, "remaining": R}}``. Malformed
    entries fail closed via :class:`SpellError`, never guessed counts.
    """
    raw = store.get("spell_slots") if isinstance(store, dict) else getattr(store, "spell_slots", None)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SpellError(
            "malformed_spell_slots",
            f"spell_slots must be an object, got {type(raw).__name__}",
            field="spell_slots",
        )
    out: dict[str, dict[str, int]] = {}
    for level, value in raw.items():
        key = str(level)
        if not key.isdigit() or not 1 <= int(key) <= 9:
            raise SpellError("unknown_slot_level", f"slot level {level!r} outside 1-9", field="spell_slots")
        if isinstance(value, int):
            if value < 0:
                raise SpellError("malformed_spell_slots", f"slot level {key} count {value} < 0", field="spell_slots")
            out[key] = {"max": value, "used": 0, "remaining": value}
        elif isinstance(value, dict):
            try:
                maximum = int(value.get("max", value.get("maximum", 0)) or 0)
                used = int(value.get("used", 0) or 0)
            except (TypeError, ValueError):
                raise SpellError("malformed_spell_slots", f"slot level {key} counts are malformed", field="spell_slots") from None
            if maximum < 0 or used < 0 or used > maximum:
                raise SpellError(
                    "malformed_spell_slots",
                    f"slot level {key} has used {used} > max {maximum}",
                    field="spell_slots",
                )
            out[key] = {"max": maximum, "used": used, "remaining": maximum - used}
        else:
            raise SpellError("malformed_spell_slots", f"slot level {key} entry is malformed", field="spell_slots")
    return out


def slot_remaining(slots: dict[str, dict[str, int]], level: int) -> int:
    entry = slots.get(str(level))
    return int(entry["remaining"]) if entry else 0


# ── Damage / healing expressions ──────────────────────────────────────────


def cantrip_dice_count(character_level: int) -> int:
    """Cantrip damage dice by character level (2024 scaling: 1/5/11/17)."""
    if type(character_level) is not int or not 1 <= character_level <= 20:
        raise SpellError("invalid_level", f"character level must be an integer 1-20, got {character_level!r}", field="level")
    return 1 + (1 if character_level >= 5 else 0) + (1 if character_level >= 11 else 0) + (1 if character_level >= 17 else 0)


def _scale_expression(base: str, *, dice: int) -> str:
    """Rebuild a strict NdM[+-K] expression with a replacement die count."""
    from app.rules.attacks import parse_damage_expression

    spec = parse_damage_expression(base)
    mod = f"+{spec.modifier}" if spec.modifier > 0 else (f"{spec.modifier}" if spec.modifier < 0 else "")
    return f"{dice}d{spec.die_size}{mod}"


def _upcast_expression(base: str, extra: str, steps: int) -> str:
    """Add ``steps`` copies of an upcast die expression to a base expression."""
    from app.rules.attacks import parse_damage_expression

    if steps <= 0:
        return base
    base_spec = parse_damage_expression(base)
    extra_spec = parse_damage_expression(extra)
    if extra_spec.die_size != base_spec.die_size:
        raise SpellError(
            "invalid_upcast",
            f"upcast die d{extra_spec.die_size} does not match base d{base_spec.die_size}",
            field="slot_level",
        )
    total_dice = base_spec.num_dice + extra_spec.num_dice * steps
    total_mod = base_spec.modifier + extra_spec.modifier * steps
    mod = f"+{total_mod}" if total_mod > 0 else (f"{total_mod}" if total_mod < 0 else "")
    return f"{total_dice}d{base_spec.die_size}{mod}"


def damage_expression_for_slot(
    definition: SpellDef,
    slot_level: int | None,
    *,
    character_level: int = 1,
) -> str:
    """Deterministic damage expression for a cast (cantrip scaling + upcasting)."""
    if definition.damage_base is None:
        raise SpellError("no_damage", f"spell {definition.name!r} deals no damage", field="spell")
    if definition.level == 0:
        return _scale_expression(definition.damage_base, dice=cantrip_dice_count(character_level))
    if slot_level is None:
        raise SpellError("missing_slot_level", f"spell {definition.name!r} requires a slot level", field="slot_level")
    if type(slot_level) is not int or slot_level < definition.level or slot_level > 9:
        raise SpellError(
            "invalid_slot_level",
            f"spell {definition.name!r} (level {definition.level}) cannot use slot {slot_level!r}",
            field="slot_level",
        )
    steps = slot_level - definition.level
    if steps and definition.damage_upcast is None:
        raise SpellError(
            "no_upcast",
            f"spell {definition.name!r} cannot be upcast to slot {slot_level}",
            field="slot_level",
        )
    return _upcast_expression(definition.damage_base, definition.damage_upcast or definition.damage_base, steps)


def healing_expression_for_slot(
    definition: SpellDef,
    slot_level: int | None,
    *,
    ability_modifier: int = 0,
) -> str:
    """Deterministic healing expression for a cast (upcast dice + ability mod)."""
    if definition.healing_base is None:
        raise SpellError("no_healing", f"spell {definition.name!r} restores no hit points", field="spell")
    if type(ability_modifier) is not int:
        raise SpellError("invalid_modifier", "ability_modifier must be int", field="ability_modifier")
    if definition.level == 0:
        base = definition.healing_base
    else:
        if slot_level is None:
            raise SpellError("missing_slot_level", f"spell {definition.name!r} requires a slot level", field="slot_level")
        if type(slot_level) is not int or slot_level < definition.level or slot_level > 9:
            raise SpellError(
                "invalid_slot_level",
                f"spell {definition.name!r} (level {definition.level}) cannot use slot {slot_level!r}",
                field="slot_level",
            )
        steps = slot_level - definition.level
        base = definition.healing_base
        if steps:
            if definition.healing_upcast is None:
                raise SpellError("no_upcast", f"spell {definition.name!r} cannot be upcast", field="slot_level")
            base = _upcast_expression(base, definition.healing_upcast, steps)
    from app.rules.attacks import parse_damage_expression

    spec = parse_damage_expression(base)
    total_mod = spec.modifier + ability_modifier
    mod = f"+{total_mod}" if total_mod > 0 else (f"{total_mod}" if total_mod < 0 else "")
    return f"{spec.num_dice}d{spec.die_size}{mod}"


# ── Cast validation ───────────────────────────────────────────────────────


class ActionEconomy(StrictModel):
    """Baseline action/casting requirements for one cast attempt."""

    has_action: bool = True
    has_bonus_action: bool = True
    has_reaction: bool = False
    incapacitated: bool = False


class SpellValidation(StrictModel):
    """Result of validating one cast before any staging or narration."""

    valid: bool
    spell_key: str
    spell_name: str
    cast_slot_level: int | None = None
    concentration_op: Literal["none", "start", "replace"] = "none"
    concentration_broke: str | None = None
    fallback: dict[str, Any] | None = None
    rule_ref: dict[str, str] = Field(default_factory=dict)
    failure_code: str | None = None
    failure_detail: str | None = None


class SpellFallback(StrictModel):
    """Structured long-tail fallback: DM + rules retrieval, never faked support."""

    spell_key: str
    spell_name: str
    reason: str
    retrieval_query: str
    rule_ref: dict[str, str] = Field(default_factory=dict)
    consumes_resources: bool = False  # invariant: fallback never stages spends


def fallback_for_spell(name: Any, reason: str) -> SpellFallback:
    key = normalize_spell_name(name) if isinstance(name, str) and name.strip() else "unknown"
    ref = spell_rule_ref(name)
    return SpellFallback(
        spell_key=key,
        spell_name=str(name).strip() if isinstance(name, str) else str(name),
        reason=reason,
        retrieval_query=str(ref.get("retrieval_query") or f"2024 spell {name}"),
        rule_ref=ref,
    )


def _holder_spells(holder: Any, caster_kind: CasterKind) -> KnownSpells:
    if caster_kind == "npc":
        if not isinstance(holder, dict):
            raise SpellError("missing_mechanical_input", "NPC casts require WorldEntity.details", field="caster")
        return query_npc_spells(holder)
    return query_known_spells(holder)


def _holder_slots(holder: Any, caster_kind: CasterKind) -> dict[str, dict[str, int]]:
    if caster_kind == "npc":
        if not isinstance(holder, dict):
            raise SpellError("missing_mechanical_input", "NPC casts require WorldEntity.details", field="caster")
        return query_spell_slots(holder)
    return query_spell_slots(holder)


def _holder_concentration(holder: Any, caster_kind: CasterKind) -> dict[str, Any] | None:
    if caster_kind == "npc":
        if not isinstance(holder, dict):
            return None
        raw = holder.get("concentration")
        return dict(raw) if isinstance(raw, dict) else None
    extras = getattr(holder, "extras", None)
    if isinstance(extras, dict) and isinstance(extras.get("concentration"), dict):
        return dict(extras["concentration"])
    return None


def _holder_casting_ability(holder: Any, caster_kind: CasterKind) -> str | None:
    if caster_kind == "npc":
        if isinstance(holder, dict):
            raw = holder.get("spellcasting_ability")
            if isinstance(raw, str) and raw.strip():
                from app.rules.mechanics import _norm_spell_ability

                return _norm_spell_ability(raw)
        return None
    raw = getattr(holder, "spellcasting_ability", None)
    if not raw:
        return None
    from app.rules.mechanics import _norm_spell_ability

    return _norm_spell_ability(str(raw))


def validate_spell_cast(
    holder: Any,
    spell_name: Any,
    *,
    caster_kind: CasterKind = "pc",
    slot_level: int | None = None,
    actions: ActionEconomy | None = None,
    character_level: int = 1,
) -> SpellValidation:
    """Validate whether a cast is legal with current resources. Pure.

    Returns a fallback validation (``valid=False`` + ``fallback``) for the
    unsupported long tail instead of raising; raises :class:`SpellError` for
    invalid deterministic uses (unknown spell, no slots, action conflict,
    missing casting ability). Never stages, consumes, or narrates.
    """
    from app.rules.state import parse_concentration

    t0 = time.monotonic()
    economy = actions or ActionEconomy()
    failure: SpellError | None = None
    validation: SpellValidation | None = None
    pattern = "unknown"
    path = "supported"
    try:
        if caster_kind not in ("pc", "npc"):
            raise SpellError("invalid_caster", f"caster_kind must be pc/npc, got {caster_kind!r}", field="caster")
        if type(character_level) is not int or not 1 <= character_level <= 20:
            raise SpellError("invalid_level", f"character level must be 1-20, got {character_level!r}", field="level")

        key = normalize_spell_name(spell_name)
        if key not in SUPPORTED_SPELLS:
            path = "fallback"
            fallback = fallback_for_spell(spell_name, "no deterministic launch support for this spell")
            return SpellValidation(
                valid=False,
                spell_key=fallback.spell_key,
                spell_name=fallback.spell_name,
                fallback=fallback.model_dump(mode="json"),
                rule_ref=fallback.rule_ref,
                failure_code="unsupported_spell",
                failure_detail=fallback.reason,
            )
        definition = SUPPORTED_SPELLS[key]
        pattern = ",".join(definition.patterns)

        # Known / prepared from existing sheet data — fail closed.
        # Castability comes from the normalized prepared set: string
        # shorthand and dicts without an explicit ``prepared: false`` count
        # as castable, while ``prepared: false`` entries are known but not
        # castable. A decision model can never make an illegal action legal.
        known = _holder_spells(holder, caster_kind)
        display = str(spell_name).strip().lower()
        known_names = [definition.name.lower(), definition.key.replace("_", " "), definition.key]
        if not known.lists_present:
            raise SpellError(
                "spell_not_known",
                f"no spell lists on record: cannot confirm {definition.name!r} is known or prepared",
                field="spell",
                details={"spell": definition.name},
            )
        castable: bool
        known_hit = any(n in known.cantrips or n in known.spells for n in known_names)
        if definition.level == 0:
            # Cantrips need no preparation: being known is sufficient, so the
            # frontend editor's default ``is_prepared: false`` never blocks a
            # known cantrip (e.g. a ``spell_level: 0`` row in ``spells``).
            castable = known_hit or display in known.cantrips or display in known.spells
        else:
            castable = any(n in known.prepared for n in known_names) or display in known.prepared
        if not castable:
            if known_hit:
                raise SpellError(
                    "spell_not_prepared",
                    f"spell {definition.name!r} is known but not prepared",
                    field="spell",
                    details={"spell": definition.name},
                )
            raise SpellError(
                "spell_not_known",
                f"spell {definition.name!r} is not on the known/prepared list",
                field="spell",
                details={"spell": definition.name},
            )

        # Casting ability must exist (DC / attack derive from it).
        if _holder_casting_ability(holder, caster_kind) is None:
            raise SpellError(
                "no_spellcasting_ability",
                "caster has no spellcasting ability: cannot set a save DC or spell attack bonus",
                field="spellcasting_ability",
            )

        # Baseline action / casting requirements.
        if economy.incapacitated:
            raise SpellError(
                "cannot_act",
                "an incapacitated caster cannot cast spells",
                field="actions",
                details={"spell": definition.name},
            )
        if definition.casting_time == "action" and not economy.has_action:
            raise SpellError("action_required", f"spell {definition.name!r} requires an action", field="actions")
        if definition.casting_time == "bonus_action" and not economy.has_bonus_action:
            raise SpellError("bonus_action_required", f"spell {definition.name!r} requires a bonus action", field="actions")
        if definition.casting_time == "reaction" and not economy.has_reaction:
            raise SpellError("reaction_required", f"spell {definition.name!r} requires a reaction", field="actions")

        # Slot / resource availability.
        cast_slot: int | None = None
        if definition.level == 0:
            if slot_level not in (None, 0):
                raise SpellError("no_slot_required", f"cantrip {definition.name!r} uses no spell slot", field="slot_level")
        else:
            slots = _holder_slots(holder, caster_kind)
            cast_slot = slot_level if slot_level is not None else definition.level
            if type(cast_slot) is not int or cast_slot < definition.level or cast_slot > 9:
                raise SpellError(
                    "invalid_slot_level",
                    f"spell {definition.name!r} (level {definition.level}) cannot use slot {slot_level!r}",
                    field="slot_level",
                )
            if definition.damage_upcast is None and definition.healing_upcast is None and cast_slot != definition.level and "damage" not in definition.patterns and "healing" not in definition.patterns:
                raise SpellError("no_upcast", f"spell {definition.name!r} cannot be upcast", field="slot_level")
            if slot_remaining(slots, cast_slot) < 1:
                raise SpellError(
                    "insufficient_slot",
                    f"no level-{cast_slot} spell slots remaining for {definition.name!r}",
                    field="slot_level",
                    details={"slot_level": cast_slot, "remaining": slot_remaining(slots, cast_slot)},
                )

        # Concentration conflicts (code-owned #227 state, read-only here).
        conc_op: Literal["none", "start", "replace"] = "none"
        broke: str | None = None
        if definition.concentration:
            current = parse_concentration(_holder_concentration(holder, caster_kind))
            if current.active:
                conc_op = "replace"
                broke = current.effect_name
            else:
                conc_op = "start"

        # Validate derivable expressions up front so nothing stages for a
        # spell whose resolution shape is broken.
        if "damage" in definition.patterns and definition.damage_base is not None:
            damage_expression_for_slot(definition, cast_slot, character_level=character_level)
        if "healing" in definition.patterns and definition.healing_base is not None:
            ability_mod = 0
            if caster_kind == "pc":
                try:
                    mechanics = get_character_mechanics_for_sheet(holder)
                    ability = _holder_casting_ability(holder, caster_kind)
                    if ability and ability in mechanics.abilities:
                        ability_mod = mechanics.abilities[ability].modifier
                except MechanicsError as exc:
                    raise SpellError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
            healing_expression_for_slot(definition, cast_slot, ability_modifier=ability_mod)

        validation = SpellValidation(
            valid=True,
            spell_key=definition.key,
            spell_name=definition.name,
            cast_slot_level=cast_slot,
            concentration_op=conc_op,
            concentration_broke=broke,
            rule_ref=spell_rule_ref(definition.name),
        )
        return validation
    except SpellError as exc:
        failure = exc
        _observe(
            exc.code,
            spell=str(spell_name),
            caster_kind=caster_kind,
            spell_pattern=pattern,
            path=path,
            latency_ms=round((time.monotonic() - t0) * 1000, 2),
            spells_version=SPELLS_VERSION,
        )
        raise
    finally:
        if validation is not None:
            try:
                structured_log(
                    logger, logging.INFO, "spellcasting_validation",
                    spell=validation.spell_name, caster_kind=caster_kind,
                    spell_pattern=pattern, path=path,
                    cast_slot_level=validation.cast_slot_level,
                    concentration_op=validation.concentration_op,
                    latency_ms=round((time.monotonic() - t0) * 1000, 2),
                    spells_version=SPELLS_VERSION,
                )
            except Exception:
                pass


# ── Staged cast (stage → commit; idempotent) ───────────────────────────────


class StagedSpellCast(StrictModel):
    """Pre-commit staged changes for one validated cast.

    ``staged_effects`` are #206-registry-compatible effect records (slot
    spend via ``apply_resource``, concentration via ``apply_concentration``,
    conditions via ``apply_condition``, damage via ``apply_attack_damage``).
    Healing stays a pure resolution primitive (:func:`resolve_spell_healing`)
    until the integration lane registers its staged commit shape — no staged
    record is emitted for healing here. All IDs derive
    deterministically from ``cast_id`` so a duplicate retry stages identical
    records and replays instead of double-applying. ``resolution_plan``
    tells the caller which #225/#226 resolvers to run for the effect shape.
    """

    cast_id: str
    spell_key: str
    spell_name: str
    cast_slot_level: int | None = None
    staged_effects: list[dict[str, Any]] = Field(default_factory=list)
    resolution_plan: dict[str, Any] = Field(default_factory=dict)
    rule_ref: dict[str, str] = Field(default_factory=dict)
    npc_private: bool = False


_CAST_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _require_cast_id(cast_id: str | None) -> str:
    if not isinstance(cast_id, str) or not cast_id.strip() or len(cast_id) > 48 or not _CAST_ID_RE.fullmatch(cast_id):
        raise SpellError(
            "missing_cast_id",
            "cast_id is required (1-48 chars, [A-Za-z0-9_-]+): supply one stable ID per logical cast and reuse it on retry",
            field="cast_id",
        )
    return cast_id


def _derived_id(cast_id: str, suffix: str) -> str:
    candidate = f"{cast_id}-{suffix}"
    if len(candidate) > 48 or not _CAST_ID_RE.fullmatch(candidate):
        raise SpellError("invalid_cast_id", f"cast_id {cast_id!r} leaves no room for derived effect IDs", field="cast_id")
    return candidate


def stage_spell_cast(
    validation: SpellValidation,
    *,
    cast_id: str | None,
    caster_kind: CasterKind = "pc",
    caster_id: str,
    visibility: str = "dm_private",
    character_level: int = 1,
) -> StagedSpellCast:
    """Stage resource/effect changes for a validated cast. Pure.

    Rejects invalid uses before staging (revalidate here even when the
    caller validated earlier — a decision model can never make an illegal
    action legal). Produces no dice, no narration, and no persistence.
    """
    cid = _require_cast_id(cast_id)
    if not isinstance(validation, SpellValidation) or not validation.valid:
        raise SpellError(
            validation.failure_code or "invalid_spell_use",
            validation.failure_detail or "spell use failed validation before staging",
            field="spell",
        )
    definition = get_spell_def(validation.spell_name)
    if caster_kind not in ("pc", "npc"):
        raise SpellError("invalid_caster", f"caster_kind must be pc/npc, got {caster_kind!r}", field="caster")
    try:
        import uuid as _uuid

        _uuid.UUID(str(caster_id))
    except ValueError:
        raise SpellError("invalid_target", "caster_id must be a UUID", field="caster_id") from None

    # Slot spend and concentration start are two distinct logical rules-state
    # mutations, so each gets its own stable mutation ID: the canonical
    # #227 preflight rejects two state effects sharing one mutation ID as a
    # would-be double-apply. Both derive from cast_id, so a duplicate retry
    # still stages identical records and replays instead of double-applying.
    slot_mutation_id = _derived_id(cid, "mut-slot")
    conc_mutation_id = _derived_id(cid, "mut-conc")
    staged: list[dict[str, Any]] = []

    # Slot spend — staged, never consumed before a valid resolution exists
    # (validation above *is* the valid resolution gate).
    if validation.cast_slot_level is not None:
        from app.rules.state import build_resource_effect

        staged.append(
            build_resource_effect(
                effect_id=_derived_id(cid, "slot"),
                mutation_id=slot_mutation_id,
                target_kind=caster_kind,
                target_id=str(caster_id),
                op="spend",
                resource=f"spell_slots:{validation.cast_slot_level}",
                amount=1,
                visibility=visibility,
            )
        )

    # Concentration start/replace for concentration spells.
    if definition.concentration and validation.concentration_op in ("start", "replace"):
        from app.rules.state import build_concentration_effect

        staged.append(
            build_concentration_effect(
                effect_id=_derived_id(cid, "conc"),
                mutation_id=conc_mutation_id,
                target_kind=caster_kind,
                target_id=str(caster_id),
                op=validation.concentration_op,  # type: ignore[arg-type]
                effect_name=definition.name,
                concentration_effect_id=_derived_id(cid, "ce"),
                source=f"spell:{definition.key}",
                visibility=visibility,
                provenance={"rule_id": definition.rule_id, "cast_id": cid},
            )
        )

    plan: dict[str, Any] = {
        "patterns": list(definition.patterns),
        "needs_attack_roll": definition.attack_roll,
        "needs_save": definition.save_ability is not None,
        "save_ability": definition.save_ability,
        "needs_damage": definition.damage_base is not None,
        "needs_healing": definition.healing_base is not None,
        "applies_condition_on_failed_save": definition.condition,
        "damage_expression": None,
        "healing_expression": None,
        "area": definition.area,
        "range_ft": definition.range_ft,
        "range_text": definition.range_text,
    }
    if plan["needs_damage"]:
        plan["damage_expression"] = damage_expression_for_slot(
            definition, validation.cast_slot_level, character_level=character_level
        )
        plan["damage_type"] = definition.damage_type
    if plan["needs_healing"]:
        plan["healing_expression"] = definition.healing_base

    try:
        structured_log(
            logger, logging.INFO, "spellcasting_staged",
            cast_id=cid, spell=definition.name, caster_kind=caster_kind,
            cast_slot_level=validation.cast_slot_level,
            staged_effects=[e.get("effect_type") for e in staged],
            concentration_op=validation.concentration_op,
            spells_version=SPELLS_VERSION,
        )
    except Exception:
        pass
    return StagedSpellCast(
        cast_id=cid,
        spell_key=definition.key,
        spell_name=definition.name,
        cast_slot_level=validation.cast_slot_level,
        staged_effects=staged,
        resolution_plan=plan,
        rule_ref=spell_rule_ref(definition.name),
        npc_private=caster_kind == "npc",
    )


# ── Resolution wiring into #225 / #226 primitives ─────────────────────────


def spell_attacker(holder: Any, spell_name: Any, *, caster_kind: CasterKind = "pc"):
    """Authoritative spell-attack offense (spell attack bonus, never weapon math)."""
    from app.rules.attacks import AttackError, CombatantOffense

    definition = get_spell_def(spell_name)
    if not definition.attack_roll:
        raise SpellError("no_attack_roll", f"spell {definition.name!r} makes no attack roll", field="spell")
    if caster_kind == "pc":
        try:
            mechanics = get_character_mechanics_for_sheet(holder)
        except MechanicsError as exc:
            raise SpellError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
        bonus = mechanics.spellcasting.attack_bonus_effective
        if bonus is None:
            raise SpellError(
                "no_spellcasting_ability",
                "caster has no spell attack bonus: set a spellcasting ability",
                field="spell_attack_bonus",
            )
        return CombatantOffense(
            attack_name=definition.name,
            attack_bonus=bonus,
            calculation_path="spell_authoritative",
            provenance={
                "spell": definition.key,
                "rule_id": definition.rule_id,
                "mechanics_version": MECHANICS_VERSION,
                "rules_revision": RULES_REVISION,
            },
        )
    # NPC / sheet-less: explicit DM-supplied bonus from details.
    raw: Any = None
    if isinstance(holder, dict):
        raw = holder.get("spell_attack_bonus")
    if type(raw) is not int:
        raise SpellError(
            "missing_mechanical_input",
            "NPC spell attacks require an explicit integer spell_attack_bonus in details",
            field="spell_attack_bonus",
        )
    try:
        return CombatantOffense(
            attack_name=definition.name,
            attack_bonus=raw,
            calculation_path="dm_supplied",
            provenance={"spell": definition.key, "rule_id": definition.rule_id},
        )
    except ValueError as exc:
        raise SpellError("invalid_modifier", str(exc), field="spell_attack_bonus") from exc


def spell_save_dc(holder: Any, spell_name: Any, *, caster_kind: CasterKind = "pc") -> int:
    """Authoritative spell save DC for a supported spell."""
    definition = get_spell_def(spell_name)
    if definition.save_ability is None and "saving_throw" in definition.patterns:
        raise SpellError("no_save", f"spell {definition.name!r} sets no save DC", field="spell")
    if caster_kind == "pc":
        try:
            mechanics = get_character_mechanics_for_sheet(holder)
        except MechanicsError as exc:
            raise SpellError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
        dc = mechanics.spellcasting.save_dc_effective
        if dc is None:
            raise SpellError(
                "no_spellcasting_ability",
                "caster has no spell save DC: set a spellcasting ability",
                field="spell_save_dc",
            )
        return dc
    raw: Any = holder.get("spell_save_dc") if isinstance(holder, dict) else None
    if type(raw) is not int:
        raise SpellError(
            "missing_mechanical_input",
            "NPC spell saves require an explicit integer spell_save_dc in details",
            field="spell_save_dc",
        )
    return raw


def resolve_spell_attack(
    *,
    caster: Any,
    spell_name: Any,
    defender: Any,
    caster_kind: CasterKind = "pc",
    dice: list[int] | None = None,
    advantage_sources: list[str] | None = None,
    attack_id: str | None = None,
    rng: Any | None = None,
):
    """Resolve a spell attack roll via the #226 attack primitive. Pure."""
    from app.rules.attacks import resolve_attack_roll

    attacker = spell_attacker(caster, spell_name, caster_kind=caster_kind)
    try:
        return resolve_attack_roll(
            attacker=attacker,
            defender=defender,
            attacker_kind=caster_kind,
            dice=dice,
            advantage_sources=advantage_sources,  # type: ignore[arg-type]
            attack_id=attack_id,
            rng=rng,
        )
    except ValueError as exc:
        code = getattr(exc, "code", "attack_failed")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc


def resolve_spell_save(
    *,
    caster: Any,
    spell_name: Any,
    target: Any,
    caster_kind: CasterKind = "pc",
    target_kind: CasterKind = "pc",
    target_modifier: int | None = None,
    dice: list[int] | None = None,
    advantage_sources: list[str] | None = None,
    roll_id: str | None = None,
    rng: Any | None = None,
):
    """Resolve a target's save against a spell via the #225 save primitive. Pure.

    Either a target ``sheet`` (authoritative #224 derivation) or an explicit
    DM-supplied ``target_modifier`` is required — never both missing.
    ``target_kind`` selects the #225 roller path (PC dice are player-supplied;
    NPC dice may be DM-supplied or runtime-generated) and die visibility, so a
    PC casting at an NPC never requires player dice for the NPC's save. The
    caster's DC stays public for PC casters and DM-private for NPC casters.
    """
    from app.rules.resolution import resolve_saving_throw

    definition = get_spell_def(spell_name)
    if definition.save_ability is None:
        raise SpellError("no_save", f"spell {definition.name!r} allows no saving throw", field="spell")
    if target_kind not in ("pc", "npc"):
        raise SpellError("invalid_target", f"target_kind must be pc/npc, got {target_kind!r}", field="target")
    dc = spell_save_dc(caster, spell_name, caster_kind=caster_kind)
    try:
        return resolve_saving_throw(
            ability=definition.save_ability,
            roller=target_kind,
            sheet=target,
            modifier=target_modifier,
            dice=dice,
            advantage_sources=advantage_sources,  # type: ignore[arg-type]
            dc=dc,
            dc_visibility="public" if caster_kind == "pc" else "hidden",
            die_visibility="public" if target_kind == "pc" else "hidden",
            roll_id=roll_id,
            rng=rng,
        )
    except ValueError as exc:
        code = getattr(exc, "code", "save_failed")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc


def resolve_spell_damage(
    *,
    spell_name: Any,
    slot_level: int | None,
    damage_rolls: list[int] | None = None,
    attacker_kind: CasterKind = "pc",
    is_critical: bool = False,
    defender: Any | None = None,
    damage_id: str | None = None,
    character_level: int = 1,
    rng: Any | None = None,
):
    """Resolve spell damage dice via the #226 damage primitive. Pure."""
    from app.rules.attacks import parse_damage_expression, resolve_damage

    definition = get_spell_def(spell_name)
    expression = damage_expression_for_slot(definition, slot_level, character_level=character_level)
    spec = parse_damage_expression(expression, damage_type=definition.damage_type or "untyped")
    try:
        return resolve_damage(
            spec=spec,
            damage_rolls=damage_rolls,
            attacker_kind=attacker_kind,  # type: ignore[arg-type]
            is_critical=is_critical,
            defender=defender,
            damage_id=damage_id,
            rng=rng,
        )
    except ValueError as exc:
        code = getattr(exc, "code", "damage_failed")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc


def resolve_spell_healing(
    *,
    spell_name: Any,
    slot_level: int | None,
    heal_rolls: list[int] | None = None,
    caster_kind: CasterKind = "pc",
    ability_modifier: int = 0,
    current_hp: Any,
    damage_id: str | None = None,
    change_id: str | None = None,
    rng: Any | None = None,
) -> tuple[Any, Any]:
    """Resolve spell healing deterministically. Pure.

    Dice math runs through the #226 damage primitive (receiptable,
    PC-supplied dice); HP arithmetic runs through code-owned
    :func:`app.rules.attacks.heal_damage` against the caller-supplied
    authoritative HP snapshot. Returns ``(dice_receipt, hp_change)``.
    """
    from app.rules.attacks import HitPoints, heal_damage, parse_damage_expression, resolve_damage

    definition = get_spell_def(spell_name)
    expression = healing_expression_for_slot(definition, slot_level, ability_modifier=ability_modifier)
    spec = parse_damage_expression(expression)
    if damage_id is None or change_id is None:
        raise SpellError(
            "missing_stable_id",
            "healing resolution requires stable damage_id and change_id (one per logical heal, reused on retry)",
            field="damage_id",
        )
    try:
        receipt = resolve_damage(
            spec=spec,
            damage_rolls=heal_rolls,
            attacker_kind=caster_kind,  # type: ignore[arg-type]
            damage_id=damage_id,
            rng=rng,
        )
    except ValueError as exc:
        code = getattr(exc, "code", "damage_failed")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc
    try:
        snapshot = HitPoints.model_validate(
            {"current": current_hp.current, "maximum": current_hp.maximum, "temporary": current_hp.temporary}
            if not isinstance(current_hp, dict)
            else current_hp
        )
    except Exception as exc:
        raise SpellError("invalid_hp_snapshot", f"authoritative HP snapshot is malformed: {exc}", field="hit_points") from exc
    try:
        change = heal_damage(snapshot, receipt.final_total, change_id=change_id)
    except ValueError as exc:
        code = getattr(exc, "code", "heal_failed")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc
    return receipt, change


def build_spell_damage_effect(
    *,
    effect_id: str,
    target_kind: CasterKind,
    target_id: str,
    damage: Any,
    attack: Any | None = None,
    visibility: str = "dm_private",
) -> dict[str, Any]:
    """Staged ``apply_attack_damage`` for resolved spell damage (code-built only)."""
    from app.rules.attacks import build_damage_effect

    try:
        return build_damage_effect(
            effect_id=effect_id,
            target_kind=target_kind,  # type: ignore[arg-type]
            target_id=target_id,
            damage=damage,
            attack=attack,
            visibility=visibility,  # type: ignore[arg-type]
        )
    except ValueError as exc:
        code = getattr(exc, "code", "invalid_effect")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc


def build_spell_condition_effect(
    *,
    effect_id: str,
    mutation_id: str,
    target_kind: CasterKind,
    target_id: str,
    spell_name: Any,
    save_failed: bool,
    save_dc: int | None = None,
    duration_rounds: int | None = None,
    visibility: str = "dm_private",
) -> dict[str, Any] | None:
    """Staged ``apply_condition`` for a failed-save spell condition.

    Returns ``None`` when the save succeeded (no condition applies) — the
    caller still holds the slot spend + concentration stages. Raises
    :class:`SpellError` when the spell applies no condition.
    """
    from app.rules.state import build_condition_effect

    definition = get_spell_def(spell_name)
    if definition.condition is None:
        raise SpellError("no_condition", f"spell {definition.name!r} applies no condition", field="spell")
    if not save_failed:
        return None
    save_ends = {"ability": definition.save_ability or "wisdom", "dc": save_dc} if save_dc is not None else None
    try:
        return build_condition_effect(
            effect_id=effect_id,
            mutation_id=mutation_id,
            target_kind=target_kind,  # type: ignore[arg-type]
            target_id=target_id,
            op="add",
            condition=definition.condition,
            source=f"spell:{definition.key}",
            duration_rounds=duration_rounds,
            save_ends=save_ends,
            visibility=visibility,  # type: ignore[arg-type]
            provenance={"rule_id": definition.rule_id},
        )
    except ValueError as exc:
        code = getattr(exc, "code", "invalid_effect")
        raise SpellError(code, str(exc), field=getattr(exc, "field", None), details=dict(getattr(exc, "details", {}))) from exc


# ── Duplicate-application guard ───────────────────────────────────────────


class SpellLedger:
    """Process-local guard so one logical cast cannot stage twice in-process.

    Same scope warning as #225/#226/#227 ledgers: in-memory only — for
    crash-safe / multi-worker idempotency use :func:`apply_spell_consequence`,
    which persists the consumed ``cast_id`` via the existing
    ``app.idempotency`` durable command record and replays on retry.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def apply(self, cast_id: str) -> Literal["applied", "duplicate"]:
        if cast_id in self._applied:
            return "duplicate"
        self._applied.add(cast_id)
        return "applied"

    def is_duplicate(self, cast_id: str) -> bool:
        return cast_id in self._applied

    def __len__(self) -> int:
        return len(self._applied)


def apply_spell_consequence(
    db: Any,
    *,
    actor_id: Any,
    scope_type: str,
    scope_id: Any,
    cast_id: str,
    payload: dict[str, Any],
    execute: Any,
    command_type: str = "spellcasting.apply",
) -> tuple[Any, bool]:
    """Durably apply one logical cast's staged consequence exactly once.

    Keyed by the stable ``cast_id`` (caller-minted on first attempt, reused
    on retry) via the existing ``app.idempotency`` durable command record.
    Returns ``(result, replayed)``.
    """
    from app.idempotency import execute_idempotent_command

    t0 = time.monotonic()
    try:
        result, replayed = execute_idempotent_command(
            db,
            actor_id=actor_id,
            idempotency_key=_require_cast_id(cast_id),
            command_type=command_type,
            scope_type=scope_type,
            scope_id=scope_id,
            payload=payload,
            execute=execute,
        )
    finally:
        try:
            structured_log(
                logger, logging.INFO, "spellcasting_idempotent_apply",
                cast_id=cast_id, command_type=command_type,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
                spells_version=SPELLS_VERSION,
            )
        except Exception:
            pass
    return result, replayed


# ── Domain event ──────────────────────────────────────────────────────────

SPELL_CAST_EVENT = "spell_cast.staged"


def spell_domain_event(staged: StagedSpellCast, *, include_private: bool) -> tuple[str, dict[str, Any], str]:
    """(event_type, payload, visibility) triple for commit_campaign_mutation.

    A private payload always pairs with ``dm_private`` visibility. The public
    projection names the spell + slot only — hidden NPC spell specifics stay
    DM-only while the fact of a cast is still recorded.
    """
    if include_private:
        return (
            SPELL_CAST_EVENT,
            {**staged.model_dump(mode="json"), "rules_revision": RULES_REVISION, "spells_version": SPELLS_VERSION},
            "dm_private",
        )
    public: dict[str, Any] = {
        "cast_id": staged.cast_id,
        "spell_name": staged.spell_name,
        "cast_slot_level": staged.cast_slot_level,
        "resolution_plan": {
            "patterns": staged.resolution_plan.get("patterns"),
            "needs_attack_roll": staged.resolution_plan.get("needs_attack_roll"),
            "needs_save": staged.resolution_plan.get("needs_save"),
        },
    }
    if staged.npc_private:
        public["redacted"] = True
    return (SPELL_CAST_EVENT, public, "dm_private" if staged.npc_private else "public")
