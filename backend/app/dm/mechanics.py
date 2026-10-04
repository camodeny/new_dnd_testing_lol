"""Mechanics intents → code-built rules effects (issue #229).

The DM contract carries :class:`~app.dm.contract.MechanicIntent` records:
*what* should happen (damage from a hazard, a condition, a resource spend),
never the numbers. :func:`resolve_mechanics` turns them into the
``apply_*`` staged effects that ``dm.effects`` promotes at commit:

- loads the target through the same roster/campaign-scoped loader the
  commit handlers use (``effects.load_state_target``);
- checks legality by dry-running the pure ``rules.state`` / ``rules.attacks``
  transitions against current state, cumulatively across the turn's intents
  (two slot spends on one caster see each other);
- rolls damage dice with an RNG seeded from the turn and intent, so the
  validation dry run, the staging run, and a narration-only retry of the same
  turn all see identical dice;
- returns public outcome claims so narration describes what code resolved.

Illegal intents come back as :class:`MechanicIssue` records. The validator
pipeline turns them into regeneration feedback before anything is visible,
and nothing is consumed. Commit-time handlers re-check overdrafts under the
row lock.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.dm.contract import Beat, Claim, DmTurnContractV1, EntityRef, MechanicIntent
from app.rules.attacks import (
    AttackError,
    CombatantDefense,
    HitPoints,
    apply_damage,
    heal_damage,
    build_damage_effect,
    hp_from_npc,
    parse_damage_expression,
    resolve_damage,
)
from app.rules.state import (
    StateError,
    add_condition,
    build_condition_effect,
    build_death_save_effect,
    build_resource_effect,
    normalize_condition_name,
    remove_condition,
    spend_resource,
    spend_spell_slot,
)

#: Beat id for the code-authored outcome beat appended to the contract.
OUTCOME_BEAT_ID = "mechanics_outcome"


@dataclass
class MechanicIssue:
    """One intent code refused; ``message`` is DM-facing retry feedback."""

    intent_id: str
    code: str
    message: str


@dataclass
class MechanicsResolution:
    effects: list[dict[str, Any]] = field(default_factory=list)
    outcomes: list[tuple[MechanicIntent, str]] = field(default_factory=list)
    issues: list[MechanicIssue] = field(default_factory=list)


def resolve_mechanics(db: Session, campaign: Any, turn: Any, contract: DmTurnContractV1) -> MechanicsResolution:
    """Resolve every intent; pure with respect to stored state (no writes)."""
    from app.dm.effects import load_state_target

    resolution = MechanicsResolution()
    targets: dict[tuple[str, str], Any] = {}
    shared = (getattr(turn, "audience", None) or "campaign") == "campaign"
    for intent in contract.mechanics:
        kind = "pc" if intent.target.type == "character" else "npc"
        key = (kind, str(intent.target.id))
        try:
            if key not in targets:
                targets[key] = load_state_target(db, campaign, kind, intent.target.id, label=f"mechanic {intent.id!r}")
            target = targets[key]
        except ValueError as exc:
            resolution.issues.append(MechanicIssue(intent.id, "unknown_target", str(exc)))
            continue
        # A PC's own state is table-visible on a shared turn; NPC rules state
        # stays DM-only (the outcome claim carries the public part).
        visibility = "public" if kind == "pc" and shared else "dm_private"
        effect_id = f"mech-{intent.id}"
        mutation_id = f"{turn.id}:{intent.id}"
        try:
            if intent.kind == "damage":
                effect, outcome = _resolve_damage(turn, intent, target, kind, effect_id, visibility)
            elif intent.kind == "heal":
                effect, outcome = _resolve_heal(turn, intent, target, kind, effect_id, mutation_id, visibility, resolution)
            elif intent.kind == "condition":
                effect, outcome = _resolve_condition(intent, target, kind, effect_id, mutation_id, visibility)
            else:
                effect, outcome = _resolve_spend(intent, target, kind, effect_id, mutation_id, visibility)
        except (StateError, AttackError) as exc:
            resolution.issues.append(MechanicIssue(intent.id, exc.code, str(exc)))
            continue
        resolution.effects.append(effect)
        resolution.outcomes.append((intent, outcome))
    return resolution


def with_outcome_beat(contract: DmTurnContractV1, resolution: MechanicsResolution) -> DmTurnContractV1:
    """Contract with one code-authored beat stating the resolved outcomes.

    Replaces any earlier outcome beat, so re-resolving a snapshot (narration
    retry) is idempotent. Narration grounds numbers and consequences in beat
    claims, so this is what lets the narrator say "7 fire damage".
    """
    beats = [b for b in contract.beats if b.id != OUTCOME_BEAT_ID]
    if resolution.outcomes:
        beats.append(Beat(
            id=OUTCOME_BEAT_ID,
            type="narration",
            claims=[
                Claim(
                    text=text,
                    claim_kind="world_fact",
                    target_refs=[EntityRef(type=intent.target.type, id=intent.target.id)],
                    evidence_refs=[f"mechanic:{intent.id}"],
                    origin="resolver_evidence",
                )
                for intent, text in resolution.outcomes
            ],
        ))
    return contract.model_copy(update={"beats": beats})


def issue_summary(issues: list[MechanicIssue]) -> str:
    return "; ".join(f"{i.intent_id}: {i.code}: {i.message}" for i in issues)


# ── Per-kind resolution ───────────────────────────────────────────────────


def _resolve_damage(turn, intent: MechanicIntent, target, kind: str, effect_id: str, visibility: str):
    spec = parse_damage_expression(intent.damage_dice or "", damage_type=intent.damage_type or "untyped")
    if kind == "pc":
        row = target.row
        hp = HitPoints(
            current=target.hp_current,
            maximum=int(row.hit_points_max),
            temporary=int(row.hit_points_temp or 0),
        )
        defense = _damage_defense({})
    else:
        if target.hp_current is None:
            raise AttackError(
                "missing_stat",
                f"{target.name} has no tracked hit points, so code cannot apply damage to it: "
                "drop this mechanic and narrate the harm instead",
                field="hit_points",
            )
        details = dict(target.row.details or {})
        hp = hp_from_npc(current=target.hp_current, details=details)
        defense = _damage_defense(details)
    # Seeded per turn + intent: validation, staging, and a narration-only
    # retry of this turn all roll the same dice for the same intent.
    seed = f"{turn.id}:{json.dumps(intent.model_dump(mode='json'), sort_keys=True)}"
    damage = resolve_damage(
        spec=spec,
        attacker_kind="npc",
        defender=defense,
        damage_id=effect_id,
        rng=random.Random(seed),
    )
    change = apply_damage(hp, damage.final_total, change_id=effect_id)
    target.hp_current = change.after.current
    effect = build_damage_effect(
        effect_id=effect_id,
        target_kind=kind,
        target_id=str(intent.target.id),
        damage=damage,
        visibility=visibility,
    )
    text = f"{target.name} takes {damage.final_total} {damage.damage_type} damage from {intent.source}."
    if change.after.current == 0 and hp.current > 0:
        text += f" {target.name} drops to 0 hit points."
    return effect, text


def _resolve_heal(turn, intent: MechanicIntent, target, kind: str, effect_id: str, mutation_id: str, visibility: str, resolution):
    hp = _target_hp(target, kind)
    spec = parse_damage_expression(intent.heal_dice or "")
    rng = random.Random(f"{turn.id}:{json.dumps(intent.model_dump(mode='json'), sort_keys=True)}")
    total = max(0, sum(rng.randint(1, spec.die_size) for _ in range(spec.num_dice)) + spec.modifier)
    change = heal_damage(hp, total, change_id=effect_id)
    target.hp_current = change.after.current
    effect = {
        "id": effect_id,
        "effect_type": "apply_healing",
        "arguments": {
            "target_kind": kind,
            "target_id": str(intent.target.id),
            "heal_total": total,
            "heal_id": effect_id,
            "visibility": visibility,
        },
    }
    restored = change.after.current - hp.current
    text = f"{target.name} regains {restored} hit points from {intent.source}."
    # 2024: regaining any HP at 0 ends dying, so the death-save counters reset.
    if kind == "pc" and hp.current == 0 and restored > 0 and (target.successes or target.failures):
        resolution.effects.append(build_death_save_effect(
            effect_id=f"{effect_id}-ds", mutation_id=f"{mutation_id}:ds", target_kind=kind,
            target_id=str(intent.target.id), op="reset", reset_reason="healed", visibility=visibility,
        ))
        target.successes = target.failures = 0
    return effect, text


def _target_hp(target, kind: str) -> HitPoints:
    if kind == "pc":
        row = target.row
        return HitPoints(current=target.hp_current, maximum=int(row.hit_points_max), temporary=int(row.hit_points_temp or 0))
    if target.hp_current is None:
        raise AttackError(
            "missing_stat",
            f"{target.name} has no tracked hit points, so code cannot change them: narrate it instead",
            field="hit_points",
        )
    return hp_from_npc(current=target.hp_current, details=dict(target.row.details or {}))


def _damage_defense(details: dict[str, Any]) -> CombatantDefense:
    """Damage-interaction lists only: landed damage never consults AC."""
    return CombatantDefense(
        armor_class=0,
        calculation_path="damage_only",
        resistances=_str_list(details.get("resistances")),
        vulnerabilities=_str_list(details.get("vulnerabilities")),
        immunities=_str_list(details.get("immunities")),
    )


def _str_list(raw: Any) -> list[str]:
    return [str(v).strip().lower() for v in raw if str(v).strip()] if isinstance(raw, list) else []


def _resolve_condition(intent: MechanicIntent, target, kind: str, effect_id: str, mutation_id: str, visibility: str):
    name = normalize_condition_name(intent.condition)
    if name == "exhaustion":
        raise StateError(
            "unsupported_condition",
            "exhaustion levels are not supported as a mechanic yet: narrate it instead",
            field="condition",
        )
    if intent.condition_op == "add":
        target.conditions, _ = add_condition(
            target.conditions, name=name, source=intent.source,
            duration_rounds=intent.duration_rounds, visibility=visibility,
            mutation_id=mutation_id,
        )
        text = f"{target.name} is now {name} ({intent.source})."
    else:
        target.conditions, _ = remove_condition(target.conditions, name=name, mutation_id=mutation_id)
        text = f"{target.name} is no longer {name}."
    effect = build_condition_effect(
        effect_id=effect_id,
        mutation_id=mutation_id,
        target_kind=kind,
        target_id=str(intent.target.id),
        op=intent.condition_op,
        condition=name,
        source=intent.source if intent.condition_op == "add" else None,
        duration_rounds=intent.duration_rounds,
        visibility=visibility,
    )
    return effect, text


def _resolve_spend(intent: MechanicIntent, target, kind: str, effect_id: str, mutation_id: str, visibility: str):
    if intent.spell_slot_level is not None:
        level = intent.spell_slot_level
        target.slots, _ = spend_spell_slot(target.slots, level=level, mutation_id=mutation_id)
        effect = build_resource_effect(
            effect_id=effect_id, mutation_id=mutation_id, target_kind=kind,
            target_id=str(intent.target.id), op="spend", slot_level=level,
            visibility=visibility,
        )
        return effect, f"{target.name} expends a level {level} spell slot ({intent.source})."
    amount = intent.amount or 1
    target.resources, _ = spend_resource(
        target.resources, name=intent.resource, amount=amount, mutation_id=mutation_id,
    )
    effect = build_resource_effect(
        effect_id=effect_id, mutation_id=mutation_id, target_kind=kind,
        target_id=str(intent.target.id), op="spend", resource=intent.resource,
        amount=amount, visibility=visibility,
    )
    return effect, f"{target.name} spends {amount} {intent.resource} ({intent.source})."
