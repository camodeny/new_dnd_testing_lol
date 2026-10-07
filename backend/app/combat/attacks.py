"""PC attack → hit → damage orchestration — issue #234.

The AI DM decides *that* a PC attacks and narrates what happened; code owns
every number in between:

1. ``plan_attack`` checks an attack roll request before it is persisted: the
   attack exists on the PC's sheet (or is a supported attack spell), the
   target has an armor class, and — in an active encounter — the attacker
   holds the active turn and the target is within reach/range (Chebyshev
   grid distance, 5 ft per square). Long range imposes disadvantage.
2. ``resolve_attack_fulfillment`` compares the player's roll against the
   target's AC via :mod:`app.rules.attacks`. The DM sees hit/miss/critical,
   never the AC.
3. On a hit the DM requests a damage roll (``plan_damage``): code supplies
   the weapon's dice, doubled on a critical hit. A miss refuses damage.
4. ``resolve_damage_fulfillment`` resolves the player's damage dice against
   the target's resistances; ``attack_damage_effects`` turns it into one
   code-built ``apply_attack_damage`` effect keyed by the damage request, so
   HP changes exactly once when the turn commits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.characters.service import latest_sheet
from app.combat.geometry import squares_to_feet
from app.rules.attacks import (
    AttackError,
    CombatantOffense,
    DamageResolution,
    attacker_from_sheet,
    build_damage_effect,
    defender_from_npc,
    defender_from_sheet,
    hp_from_npc,
    parse_damage_expression,
    resolve_attack_roll,
    resolve_damage,
)
from app.rules.mechanics import get_character_mechanics_for_sheet
from app.rules.resolution import combine_advantage
from models.combat import EncounterParticipant
from models.dm import PlayerRollFulfillment, PlayerRollRequest

MELEE_REACH_FT = 5
REACH_PROPERTY_FT = 10

_RANGE_RE = re.compile(r"range\s*\(?\s*(\d+)\s*(?:/\s*(\d+))?", re.IGNORECASE)
# Weapons that only attack at range; with no range in their properties the
# distance cannot be checked, so it is not refused.
_RANGED_NAME_RE = re.compile(r"bow|sling|dart|blowgun|musket|pistol", re.IGNORECASE)


class AttackRollError(ValueError):
    """A refused attack/damage roll; ``message`` is DM-facing feedback."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class AttackPlan:
    attack_name: str
    target_kind: str
    target_id: str
    advantage_state: str


@dataclass
class _Weapon:
    offense: CombatantOffense
    damage_dice: str | None
    damage_type: str
    reach_ft: int | None  # melee reach; None when it cannot attack in melee
    normal_ft: int | None  # ranged normal range
    long_ft: int | None  # ranged long range
    range_known: bool


def target_kind_for(ref_type: str) -> str:
    return "pc" if ref_type == "character" else "npc"


def _sheet(db: Session, character_id: Any):
    sheet = latest_sheet(db, character_id)
    if sheet is None:
        raise AttackRollError("missing_sheet", "the attacking character has no sheet, so code cannot resolve the attack")
    return sheet


def _weapon(sheet: Any, attack_name: str | None) -> _Weapon:
    """The sheet attack (or supported attack spell) being used."""
    from app.rules.spells import SpellError, damage_expression_for_slot, get_spell_def, is_spell_supported, spell_attacker

    try:
        offense = attacker_from_sheet(sheet, attack_name)
    except AttackError as exc:
        if exc.code != "missing_stat" or not attack_name or not is_spell_supported(attack_name):
            names = (exc.details or {}).get("attacks")
            hint = f"; this character's attacks are: {', '.join(names)}" if names else ""
            raise AttackRollError(exc.code, f"{exc}{hint}") from exc
        try:
            offense = spell_attacker(sheet, attack_name)
            spell = get_spell_def(attack_name)
            dice = damage_expression_for_slot(
                spell, None if spell.level == 0 else spell.level, character_level=int(sheet.level or 1),
            ) if spell.damage_base else None
        except SpellError as spell_exc:
            raise AttackRollError(spell_exc.code, str(spell_exc)) from spell_exc
        return _Weapon(
            offense=offense, damage_dice=dice, damage_type=spell.damage_type or "untyped",
            reach_ft=MELEE_REACH_FT if (spell.range_ft or 0) <= MELEE_REACH_FT else None,
            normal_ft=spell.range_ft, long_ft=spell.range_ft, range_known=spell.range_ft is not None,
        )
    detail = next(
        (a for a in get_character_mechanics_for_sheet(sheet).attacks if a.name == offense.attack_name), None,
    )
    properties = str(getattr(detail, "properties", None) or "")
    match = _RANGE_RE.search(properties)
    normal = int(match.group(1)) if match else None
    long = int(match.group(2)) if match and match.group(2) else normal
    ranged_only = bool(match) and "thrown" not in properties.lower()
    reach = None if ranged_only else (REACH_PROPERTY_FT if "reach" in properties.lower() else MELEE_REACH_FT)
    return _Weapon(
        offense=offense, damage_dice=offense.damage_expression,
        damage_type=str(getattr(detail, "damage_type", None) or "untyped"),
        reach_ft=reach, normal_ft=normal, long_ft=long,
        range_known=bool(match) or not _RANGED_NAME_RE.search(offense.attack_name),
    )


def _load_target(db: Session, campaign: Any, target_kind: str, target_id: Any):
    from app.dm.effects import load_state_target

    try:
        return load_state_target(db, campaign, target_kind, target_id, label="attack target")
    except ValueError as exc:
        raise AttackRollError("unknown_target", str(exc)) from exc


def _defense(target: Any):
    try:
        if target.kind == "pc":
            return defender_from_sheet(target.row)
        return defender_from_npc(details=dict(target.details))
    except AttackError as exc:
        if target.kind == "npc":
            raise AttackRollError(
                "missing_stat",
                f"{target.name} has no stat block, so code cannot resolve an attack against it: stage "
                "assign_stat_block with a fitting SRD monster_id in a respond turn first",
            ) from exc
        raise AttackRollError(exc.code, str(exc)) from exc


def _participant_for(participants: list[EncounterParticipant], kind: str, entity_id: str) -> EncounterParticipant | None:
    for p in participants:
        if kind == "pc" and p.character_id is not None and str(p.character_id) == entity_id:
            return p
        if kind == "npc" and p.npc_entity_id is not None and str(p.npc_entity_id) == entity_id:
            return p
    return None


def _encounter_advantage(
    db: Session, campaign: Any, character_id: Any, target_kind: str, target_id: str, weapon: _Weapon,
) -> str | None:
    """Turn/range legality inside an active encounter; returns imposed disadvantage."""
    from app.combat.maps import list_placements
    from app.combat.service import get_active_encounter, list_participants

    encounter = get_active_encounter(db, campaign.id)
    if encounter is None or encounter.status != "active":
        return None
    participants = list_participants(db, encounter.id)
    attacker = _participant_for(participants, "pc", str(character_id))
    target = _participant_for(participants, target_kind, target_id)
    if attacker is None:
        raise AttackRollError("attacker_not_in_encounter", "the attacking character is not part of the active encounter")
    if encounter.active_participant_id != attacker.id:
        raise AttackRollError(
            "not_attackers_turn",
            f"it is not {attacker.display_name}'s turn in the encounter; they can attack on their own turn",
        )
    if target is None:
        raise AttackRollError("target_not_in_encounter", "the target is not part of the active encounter")
    cells = {p.participant_id: (p.col, p.row) for p in list_placements(db, encounter.id)}
    if attacker.id not in cells or target.id not in cells or not weapon.range_known:
        return None
    (ac, ar), (tc, tr) = cells[attacker.id], cells[target.id]
    distance = squares_to_feet(max(abs(ac - tc), abs(ar - tr)))
    if weapon.reach_ft is not None and distance <= weapon.reach_ft:
        return None
    if weapon.long_ft is not None and distance <= weapon.long_ft:
        return "disadvantage" if weapon.normal_ft is not None and distance > weapon.normal_ft else None
    limit = weapon.long_ft if weapon.long_ft is not None else weapon.reach_ft
    raise AttackRollError(
        "out_of_range",
        f"{target.display_name} is {distance} ft away, beyond {weapon.offense.attack_name}'s {limit} ft "
        f"{'range' if weapon.long_ft is not None else 'reach'}; narrate the attacker closing in or "
        "choosing another target instead",
    )


def plan_attack(
    db: Session, campaign: Any, *, character_id: Any, target_ref: Any, attack_name: str | None,
    advantage_state: str = "normal",
) -> AttackPlan:
    """Check an attack roll request; raises :class:`AttackRollError`."""
    if target_ref is None:
        raise AttackRollError("missing_target", "an attack roll_request needs target_ref")
    sheet = _sheet(db, character_id)
    weapon = _weapon(sheet, attack_name)
    kind = target_kind_for(str(target_ref.type))
    target = _load_target(db, campaign, kind, target_ref.id)
    _defense(target)
    # A PC target's row is its sheet; its identity is the character id.
    target_id = str(target_ref.id) if kind == "pc" else str(target.row.id)
    imposed = _encounter_advantage(db, campaign, character_id, kind, target_id, weapon)
    state = advantage_state
    if imposed is not None:
        sources = [s for s in (advantage_state, imposed) if s != "normal"]
        state = combine_advantage(sources)  # type: ignore[arg-type]
    return AttackPlan(
        attack_name=weapon.offense.attack_name, target_kind=kind, target_id=target_id, advantage_state=state,
    )


def _fulfillment(db: Session, request_id: Any) -> PlayerRollFulfillment | None:
    return db.execute(
        select(PlayerRollFulfillment).where(PlayerRollFulfillment.roll_request_id == request_id)
    ).scalars().first()


def plan_damage(db: Session, *, turn_id: Any, attack_request_key: str | None) -> PlayerRollRequest:
    """The hit attack a damage roll follows; raises :class:`AttackRollError`."""
    if not attack_request_key:
        raise AttackRollError("missing_attack", "a damage roll_request needs attack_request_id naming the hit attack roll")
    attack = db.execute(
        select(PlayerRollRequest).where(
            PlayerRollRequest.turn_id == turn_id, PlayerRollRequest.request_key == attack_request_key,
        )
    ).scalars().first()
    if attack is None or attack.roll_kind != "attack":
        raise AttackRollError("unknown_attack", f"no attack roll {attack_request_key!r} in this turn")
    fulfillment = _fulfillment(db, attack.id)
    resolution = (fulfillment.resolution or {}) if fulfillment is not None else {}
    if resolution.get("outcome") not in ("hit", "critical"):
        raise AttackRollError("attack_missed", "that attack missed, so there is no damage to roll")
    if db.execute(select(PlayerRollRequest.id).where(PlayerRollRequest.attack_request_id == attack.id)).first():
        raise AttackRollError("damage_already_requested", "damage for that attack was already requested")
    if not resolution.get("damage_dice"):
        raise AttackRollError(
            "missing_damage_dice",
            f"{attack.attack_name} has no damage dice on the sheet; narrate the hit and add a damage mechanic instead",
        )
    return attack


def _player_dice(raw_rolls: list[int], total: int, modifier: int, *, count: int, size: int) -> list[int]:
    """The rolled faces; a physical roll reported as a total is spread canonically."""
    if raw_rolls:
        return list(raw_rolls)
    rolled = total - modifier
    if not count <= rolled <= count * size:
        raise AttackRollError(
            "invalid_total", f"a total of {total} is not possible for {count}d{size}{modifier:+d}",
        )
    faces = []
    for remaining in range(count, 0, -1):
        face = min(size, rolled - (remaining - 1))
        faces.append(face)
        rolled -= face
    return faces


def resolve_attack_fulfillment(
    db: Session, campaign: Any, req: PlayerRollRequest, *, raw_rolls: list[int], modifier: int | None, total: int,
) -> tuple[int, dict[str, Any]]:
    """``(modifier, resolution)`` for a fulfilled attack roll; never exposes AC."""
    weapon = _weapon(_sheet(db, req.character_id), req.attack_name)
    bonus = weapon.offense.attack_bonus
    if raw_rolls and modifier != bonus:
        raise AttackRollError("invalid_modifier", f"{weapon.offense.attack_name} adds {bonus:+d}, not {modifier:+d}")
    if raw_rolls:
        dice, state = raw_rolls, req.advantage_state
    else:
        dice, state = [total - bonus], "normal"  # the kept die of a physical roll
    defense = _defense(_load_target(db, campaign, req.target_kind, req.target_id))
    try:
        attack = resolve_attack_roll(
            attacker=weapon.offense, defender=defense, attacker_kind="pc", dice=dice,
            advantage_state=state, attack_id=f"atk-{req.id.hex}",
        )
    except AttackError as exc:
        raise AttackRollError(exc.code, str(exc)) from exc
    dice_code = weapon.damage_dice
    if dice_code:
        try:
            spec = parse_damage_expression(dice_code)
            mod = f"{spec.modifier:+d}" if spec.modifier else ""
            dice_code = f"{spec.num_dice * (2 if attack.is_critical else 1)}d{spec.die_size}{mod}"
        except AttackError:
            dice_code = None
    return bonus, {
        "attack_id": attack.attack_id,
        "outcome": attack.outcome,
        "is_critical": attack.is_critical,
        "damage_dice": dice_code,
        "damage_type": weapon.damage_type,
    }


def resolve_damage_fulfillment(
    db: Session, campaign: Any, req: PlayerRollRequest, *, raw_rolls: list[int], modifier: int | None, total: int,
) -> tuple[int, dict[str, Any]]:
    """``(modifier, resolution)`` for a fulfilled damage roll (applied at commit)."""
    attack = db.get(PlayerRollRequest, req.attack_request_id)
    attack_fulfillment = _fulfillment(db, req.attack_request_id) if attack is not None else None
    attack_resolution = (attack_fulfillment.resolution or {}) if attack_fulfillment is not None else {}
    spec = parse_damage_expression(req.damage_dice or "", damage_type=attack_resolution.get("damage_type") or "untyped")
    if raw_rolls and modifier != spec.modifier:
        raise AttackRollError("invalid_modifier", f"this damage adds {spec.modifier:+d}, not {modifier:+d}")
    faces = _player_dice(raw_rolls, total, spec.modifier, count=spec.num_dice, size=spec.die_size)
    target = _load_target(db, campaign, attack.target_kind, attack.target_id)
    try:
        damage = resolve_damage(
            # The request's dice already include critical doubling.
            spec=spec, damage_rolls=faces, attacker_kind="pc", defender=_defense(target),
            damage_id=f"dmg-{req.id.hex}", attack_id=attack_resolution.get("attack_id"),
        )
    except AttackError as exc:
        raise AttackRollError(exc.code, str(exc)) from exc
    return spec.modifier, {
        "target_kind": attack.target_kind,
        "target_id": attack.target_id,
        "attack_name": attack.attack_name,
        "damage": damage.model_dump(mode="json"),
    }


def attack_damage_effects(db: Session, campaign: Any, turn: Any) -> list[tuple[dict[str, Any], str, Any]]:
    """``(effect, outcome_text, target_ref)`` for each fulfilled damage roll of this turn.

    The damage was resolved when the player rolled; this only builds the
    staged effect (keyed by the damage request, so HP changes once per hit)
    and the sentence the narrator must honor.
    """
    from app.dm.contract import EntityRef

    rows = db.execute(
        select(PlayerRollRequest, PlayerRollFulfillment)
        .join(PlayerRollFulfillment, PlayerRollFulfillment.roll_request_id == PlayerRollRequest.id)
        .where(PlayerRollRequest.turn_id == turn.id, PlayerRollRequest.roll_kind == "damage")
        .order_by(PlayerRollRequest.requested_at, PlayerRollRequest.id)
    ).all()
    shared = (getattr(turn, "audience", None) or "campaign") == "campaign"
    out = []
    for req, fulfillment in rows:
        resolution = fulfillment.resolution or {}
        damage = DamageResolution.model_validate(resolution["damage"])
        kind, target_id = resolution["target_kind"], resolution["target_id"]
        target = _load_target(db, campaign, kind, target_id)
        effect = build_damage_effect(
            effect_id=damage.damage_id, target_kind=kind, target_id=target_id, damage=damage,
            visibility="public" if kind == "pc" and shared else "dm_private",
        )
        text = f"{target.name} takes {damage.final_total} {damage.damage_type} damage from {resolution['attack_name']}."
        current = target.hp_current if kind == "pc" else (
            hp_from_npc(current=target.hp_current, details=dict(target.details)).current
            if target.hp_current is not None else None
        )
        if current is not None and current > 0 and damage.final_total >= current + _temp_hp(target):
            text += f" {target.name} drops to 0 hit points."
        out.append((effect, text, EntityRef(type="character" if kind == "pc" else "npc", id=target_id)))
    return out


def _temp_hp(target: Any) -> int:
    if target.kind == "pc":
        return int(target.row.hit_points_temp or 0)
    hp = (target.details or {}).get("hit_points") or {}
    return int(hp.get("temporary") or 0) if isinstance(hp, dict) else 0


def attack_roll_modifier(sheet: Any, req: PlayerRollRequest) -> dict[str, Any] | None:
    """What the player adds (and, for damage, which dice they roll) — for the table UI."""
    try:
        if req.roll_kind == "attack":
            weapon = _weapon(sheet, req.attack_name)
            return {"modifier": weapon.offense.attack_bonus, "label": weapon.offense.attack_name}
        if req.roll_kind == "damage" and req.damage_dice:
            spec = parse_damage_expression(req.damage_dice)
            return {
                "modifier": spec.modifier, "label": f"{req.attack_name} damage",
                "dice": {"count": spec.num_dice, "sides": spec.die_size},
            }
    except (AttackRollError, AttackError):
        return None
    return None
