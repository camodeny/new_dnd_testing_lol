"""Deterministic attack/hit/damage/HP/temp-HP resolution — issue #226.

Pure calculation over authoritative mechanical inputs (#224) and explicit
dice (#225 patterns). The DM decides whether an attack is required; this
module resolves it without asking any model to perform arithmetic.

Rules-data authority: WotC SRD 5.2.1 / 2024 revision behavior is encoded
here (attack = d20 + bonus vs AC, meet-or-beat; natural 20 is a critical
hit and always hits; natural 1 always misses; critical damage rolls the
attack's damage dice twice and adds modifiers once; resistance halves
rounded down; temporary HP absorbs before current HP and never stacks).
Provenance/citation is app-owned (``rules_revision`` + ``attack_version``
on every result); search/embedding scores never enter this path.

Invariants:
- Attack bonuses and armor class come from authoritative inputs
  (sheet-derived via :mod:`app.rules.mechanics`, or explicit DM-supplied
  values for NPCs/sheet-less entities) — never both missing, never guessed.
- Player PC dice are *supplied*, never generated here. NPC/hidden dice are
  generated via the runtime path (injectable RNG) or DM-supplied.
- Advantage/disadvantage reuse the 2024 cancellation rule from #225: any
  source of each cancels to a straight roll, and sources never stack.
- Hidden AC/dice never appear in public projections or player telemetry.
- Resolution is pure — safe to retry. Duplicate *application* of one logical
  attack is guarded by :class:`AttackLedger` (process-local, single-worker
  only) or, durably across restarts/workers, by
  :func:`apply_attack_consequence`, which reuses the ``app.idempotency``
  durable command record keyed by the stable ``attack_id``.
  ``attack_id`` is caller-supplied and never minted here so a retry keeps
  the same identity and cannot bypass the guard.
- Missing inputs produce :class:`AttackError`, never guessed stats.
- Decision models (if any) only ever choose among code-supplied candidates;
  this module performs no model calls.

Out of scope: encounter turn order / range geometry (#181), spell
area/targeting, every rare damage interaction (extension hooks provided).
"""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.observability.tracing import structured_log
from app.rules.mechanics import (
    MECHANICS_VERSION,
    RULES_REVISION,
    MechanicsError,
    get_character_mechanics_for_sheet,
)
from app.rules.resolution import combine_advantage, select_d20

logger = logging.getLogger(__name__)

ATTACK_VERSION = "attack_v1"

AttackerKind = Literal["pc", "npc"]
AttackOutcome = Literal["hit", "miss", "critical"]
AdvantageState = Literal["normal", "advantage", "disadvantage"]
AdvantageSource = Literal["advantage", "disadvantage"]
DieSource = Literal["player_supplied", "dm_supplied", "runtime_generated"]
ACVisibility = Literal["public", "hidden"]
DieVisibility = Literal["public", "hidden"]

CALCULATION_PATHS = {
    "attack_authoritative",
    "dm_supplied",
}

# Domain-event types (#188 style) for callers committing through
# commit_campaign_mutation. Payloads come from the builders below.
ATTACK_RESOLVED_EVENT = "attack.resolved"
DAMAGE_APPLIED_EVENT = "damage.applied"


class AttackError(ValueError):
    """Explicit unresolved/error state — caller must surface, not guess."""

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


# ── Normalized combat-stat interface ──────────────────────────────────────
#
# PCs resolve through the authoritative sheet derivation (#224); NPCs and
# sheet-less entities resolve through explicit DM-supplied values with an
# optional entity-details dict as provenance/fallback. Both paths produce
# the same DTOs so resolution below never branches on entity kind.


class CombatantOffense(StrictModel):
    """Authoritative attacker side: validated attack bonus + provenance."""

    attack_name: str = "Unnamed Attack"
    attack_bonus: int
    calculation_path: str = "dm_supplied"
    damage_expression: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class CombatantDefense(StrictModel):
    """Authoritative defender side: validated AC + damage-interaction hooks."""

    armor_class: int
    calculation_path: str = "dm_supplied"
    resistances: list[str] = Field(default_factory=list)
    vulnerabilities: list[str] = Field(default_factory=list)
    immunities: list[str] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)


class HitPoints(StrictModel):
    """Point-in-time HP snapshot. Pure value object — never a live row ref."""

    current: int
    maximum: int
    temporary: int = 0


def _validate_bonus(value: Any, *, field: str) -> int:
    if type(value) is not int:
        raise AttackError(
            "invalid_stat", f"{field} must be an integer, got {value!r}", field=field
        )
    if not -20 <= value <= 30:
        raise AttackError(
            "invalid_stat", f"{field} {value} outside plausible -20..30", field=field
        )
    return value


def _validate_ac(value: Any, *, field: str = "armor_class") -> int:
    if type(value) is not int:
        raise AttackError(
            "invalid_stat", f"{field} must be an integer, got {value!r}", field=field
        )
    if not 1 <= value <= 40:
        raise AttackError(
            "invalid_stat", f"{field} {value} outside plausible 1..40", field=field
        )
    return value


def _validate_hp(current: Any, maximum: Any, temporary: Any) -> HitPoints:
    for name, value in (
        ("current", current),
        ("maximum", maximum),
        ("temporary", temporary),
    ):
        if type(value) is not int:
            raise AttackError(
                "invalid_stat",
                f"hit_points.{name} must be an integer, got {value!r}",
                field="hit_points",
            )
    if maximum < 1:
        raise AttackError(
            "invalid_stat",
            f"hit_points.maximum {maximum} must be >= 1",
            field="hit_points",
        )
    if current < 0 or temporary < 0:
        raise AttackError(
            "invalid_stat",
            f"hit_points.current/temporary must be >= 0, got {current}/{temporary}",
            field="hit_points",
        )
    return HitPoints(current=current, maximum=maximum, temporary=temporary)


def _norm_damage_type(raw: Any) -> str:
    return str(raw or "").strip().lower()


def _norm_damage_list(raw: Any, *, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AttackError(
            "invalid_stat", f"{field} must be a list of damage types", field=field
        )
    out: list[str] = []
    for item in raw:
        norm = _norm_damage_type(item)
        if norm:
            out.append(norm)
    return out


def attacker_from_sheet(sheet: Any, attack_name: str | None = None) -> CombatantOffense:
    """Authoritative PC offense from a character sheet (#224 derivation).

    ``attack_name`` selects among the sheet's stored attacks; when omitted a
    lone attack is used, while zero or ambiguous attacks fail closed.
    """
    try:
        mechanics = get_character_mechanics_for_sheet(sheet)
    except MechanicsError as exc:
        raise AttackError(
            exc.code, str(exc), field=exc.field, details=dict(exc.details)
        ) from exc
    attacks = list(mechanics.attacks)
    if not attacks:
        raise AttackError(
            "missing_stat",
            "sheet has no stored attacks: supply an explicit attack_bonus for this attacker",
            field="attack_bonus",
        )
    chosen = None
    if attack_name is None:
        if len(attacks) > 1:
            raise AttackError(
                "ambiguous_attack",
                f"sheet has {len(attacks)} attacks; specify attack_name",
                field="attack_name",
                details={"attacks": [a.name for a in attacks]},
            )
        chosen = attacks[0]
    else:
        wanted = attack_name.strip().lower()
        for attack in attacks:
            if attack.name.strip().lower() == wanted:
                chosen = attack
                break
        if chosen is None:
            raise AttackError(
                "missing_stat",
                f"attack {attack_name!r} not found on sheet",
                field="attack_name",
                details={"attacks": [a.name for a in attacks]},
            )
    bonus = (
        chosen.attack_bonus_effective
        if chosen.attack_bonus_effective is not None
        else chosen.attack_bonus
    )
    if bonus is None:
        raise AttackError(
            "missing_stat",
            f"attack {chosen.name!r} has no attack_bonus: supply an explicit value",
            field="attack_bonus",
        )
    return CombatantOffense(
        attack_name=chosen.name,
        attack_bonus=_validate_bonus(bonus, field="attack_bonus"),
        calculation_path="attack_authoritative",
        damage_expression=chosen.damage,
        provenance={
            "source_type": "dnd5e_character_sheet",
            "sheet_id": mechanics.meta.sheet_id,
            "sheet_version": mechanics.meta.sheet_version,
            "attack_source": chosen.source,
        },
    )


def attacker_from_npc(
    *,
    attack_bonus: int | None = None,
    attack_name: str | None = None,
    details: dict[str, Any] | None = None,
) -> CombatantOffense:
    """NPC/monster offense: explicit DM value wins, entity details fallback.

    Precedence: explicit ``attack_bonus`` > ``details`` (``attack_bonus``,
    then ``attack``) > failure. Nothing is ever defaulted to zero.
    """
    details = details or {}
    resolved = attack_bonus
    source = "dm_supplied_explicit"
    if resolved is None and isinstance(details, dict):
        for key in ("attack_bonus", "attack"):
            if details.get(key) is not None:
                resolved = details[key]
                source = "world_entity_details"
                break
    if resolved is None:
        raise AttackError(
            "missing_stat",
            "NPC attacker has no attack_bonus: supply an explicit DM value",
            field="attack_bonus",
        )
    name = (
        (attack_name or details.get("attack_name") or "Unnamed Attack")
        if isinstance(details, dict)
        else (attack_name or "Unnamed Attack")
    )
    expression = details.get("damage") if isinstance(details, dict) else None
    return CombatantOffense(
        attack_name=str(name),
        attack_bonus=_validate_bonus(resolved, field="attack_bonus"),
        calculation_path="dm_supplied",
        damage_expression=str(expression) if expression is not None else None,
        provenance={"source_type": source},
    )


def defender_from_sheet(sheet: Any) -> CombatantDefense:
    """Authoritative PC defense (AC) from a character sheet (#224)."""
    try:
        mechanics = get_character_mechanics_for_sheet(sheet)
    except MechanicsError as exc:
        raise AttackError(
            exc.code, str(exc), field=exc.field, details=dict(exc.details)
        ) from exc
    ac = mechanics.combat["armor_class"]["effective"]
    return CombatantDefense(
        armor_class=_validate_ac(ac),
        calculation_path="attack_authoritative",
        provenance={
            "source_type": "dnd5e_character_sheet",
            "sheet_id": mechanics.meta.sheet_id,
            "sheet_version": mechanics.meta.sheet_version,
        },
    )


def defender_from_npc(
    *,
    armor_class: int | None = None,
    details: dict[str, Any] | None = None,
    resistances: list[str] | None = None,
    vulnerabilities: list[str] | None = None,
    immunities: list[str] | None = None,
) -> CombatantDefense:
    """NPC/monster defense: explicit DM AC wins, entity-details fallback.

    Damage-interaction lists may come from explicit arguments or the details
    dict; explicit arguments win when both are present.
    """
    details = details or {}
    resolved = armor_class
    source = "dm_supplied_explicit"
    if resolved is None and isinstance(details, dict):
        for key in ("armor_class", "ac"):
            if details.get(key) is not None:
                resolved = details[key]
                source = "world_entity_details"
                break
    if resolved is None:
        raise AttackError(
            "missing_stat",
            "NPC defender has no armor_class: supply an explicit DM value",
            field="armor_class",
        )

    def _pick(explicit: list[str] | None, key: str) -> list[str]:
        if explicit is not None:
            return _norm_damage_list(explicit, field=key)
        return (
            _norm_damage_list(details.get(key), field=key)
            if isinstance(details, dict)
            else []
        )

    return CombatantDefense(
        armor_class=_validate_ac(resolved),
        calculation_path="dm_supplied",
        resistances=_pick(resistances, "resistances"),
        vulnerabilities=_pick(vulnerabilities, "vulnerabilities"),
        immunities=_pick(immunities, "immunities"),
        provenance={"source_type": source},
    )


def hp_from_sheet(sheet: Any) -> HitPoints:
    """Authoritative PC HP snapshot from a character sheet (#224)."""
    try:
        mechanics = get_character_mechanics_for_sheet(sheet)
    except MechanicsError as exc:
        raise AttackError(
            exc.code, str(exc), field=exc.field, details=dict(exc.details)
        ) from exc
    raw = mechanics.combat["hit_points"]
    return _validate_hp(raw["current"], raw["maximum"], raw["temporary"])


def hp_from_npc(
    *,
    current: int | None = None,
    maximum: int | None = None,
    temporary: int | None = None,
    details: dict[str, Any] | None = None,
) -> HitPoints:
    """NPC/monster HP snapshot: explicit DM values win, details fallback.

    Canonical details shape is ``details["hit_points"]`` with
    ``current``/``maximum``/``temporary`` keys; explicit arguments override
    per-field. Missing maximum fails closed.
    """
    details = details or {}
    nested = details.get("hit_points") if isinstance(details, dict) else None
    nested = nested if isinstance(nested, dict) else {}
    cur = (
        current
        if current is not None
        else nested.get("current", nested.get("current_hp"))
    )
    mx = (
        maximum
        if maximum is not None
        else nested.get("maximum", nested.get("max_hp", nested.get("max")))
    )
    tmp = (
        temporary
        if temporary is not None
        else nested.get("temporary", nested.get("temp_hp", 0))
    )
    if mx is None:
        raise AttackError(
            "missing_stat",
            "NPC has no hit-point maximum: supply explicit DM values",
            field="hit_points",
        )
    if cur is None:
        raise AttackError(
            "missing_stat",
            "NPC has no current hit points: supply explicit DM values",
            field="hit_points",
        )
    if tmp is None:
        tmp = 0
    return _validate_hp(cur, mx, tmp)


# ── Damage spec ───────────────────────────────────────────────────────────


class DamageSpec(StrictModel):
    """Validated damage dice shape: count × size + flat modifier, one type."""

    num_dice: int
    die_size: int
    modifier: int = 0
    damage_type: str = "untyped"


_DAMAGE_RE = re.compile(r"^\s*(\d+)\s*[dD]\s*(\d+)\s*([+-]\s*\d+)?\s*$")


def parse_damage_expression(
    expression: str, *, damage_type: str = "untyped"
) -> DamageSpec:
    """Parse a strict ``NdM[+-K]`` damage expression. No bare modifiers."""
    if not isinstance(expression, str):
        raise AttackError(
            "invalid_damage_expression",
            f"damage expression must be a string, got {expression!r}",
            field="damage",
        )
    match = _DAMAGE_RE.match(expression)
    if not match:
        raise AttackError(
            "invalid_damage_expression",
            f"damage expression must look like '1d8+3', got {expression!r}",
            field="damage",
        )
    num_dice, die_size = int(match.group(1)), int(match.group(2))
    modifier = int(match.group(3).replace(" ", "")) if match.group(3) else 0
    return make_damage_spec(
        num_dice=num_dice, die_size=die_size, modifier=modifier, damage_type=damage_type
    )


def make_damage_spec(
    *, num_dice: int, die_size: int, modifier: int = 0, damage_type: str = "untyped"
) -> DamageSpec:
    if type(num_dice) is not int or not 1 <= num_dice <= 100:
        raise AttackError(
            "invalid_damage_expression",
            f"num_dice must be an integer 1-100, got {num_dice!r}",
            field="damage",
        )
    if type(die_size) is not int or die_size not in (4, 6, 8, 10, 12, 20, 100):
        raise AttackError(
            "invalid_damage_expression",
            f"die_size must be a real die (d4-d20, d100), got {die_size!r}",
            field="damage",
        )
    if type(modifier) is not int or not -100 <= modifier <= 100:
        raise AttackError(
            "invalid_damage_expression",
            f"damage modifier must be an integer -100..100, got {modifier!r}",
            field="damage",
        )
    return DamageSpec(
        num_dice=num_dice,
        die_size=die_size,
        modifier=modifier,
        damage_type=_norm_damage_type(damage_type) or "untyped",
    )


# ── Damage-modifier extension hooks ───────────────────────────────────────
#
# Bounded semantic hook point for later features (resistance bypasses,
# situational damage riders). Hooks only add flat bonuses among
# code-supplied candidates; legality stays in code.


class DamageHookContext(StrictModel):
    damage_type: str = "untyped"
    is_critical: bool = False
    base_damage: int = 0


class DamageContribution(StrictModel):
    name: str
    value: int
    source: str = "situational"


class DamageHook(Protocol):
    def bonus(self, context: DamageHookContext) -> int: ...


def _hook_bonus(
    hook: DamageHook | Callable[[DamageHookContext], int],
    context: DamageHookContext,
    name: str,
) -> DamageContribution:
    try:
        if hasattr(hook, "bonus"):
            value = hook.bonus(context)  # type: ignore[union-attr]
        else:
            value = hook(context)  # type: ignore[operator]
    except AttackError:
        raise
    except Exception as exc:
        raise AttackError(
            "hook_failed",
            f"damage hook {name!r} failed: {exc}",
            field="hooks",
            details={"hook": name},
        ) from exc
    if type(value) is not int:
        raise AttackError(
            "hook_failed",
            f"damage hook {name!r} must return int, got {type(value).__name__}",
            field="hooks",
            details={"hook": name},
        )
    type_name = type(hook).__name__
    if type_name == "function":
        resolved_name = getattr(hook, "__name__", None) or name
    elif type_name == "method":
        resolved_name = type(getattr(hook, "__self__", hook)).__name__
    else:
        resolved_name = type_name
    return DamageContribution(name=resolved_name, value=value, source="hook")


# ── Runtime dice (NPC/DM path only — never for PCs) ───────────────────────


def runtime_damage_dice(
    *, num_dice: int, die_size: int, rng: Any | None = None
) -> list[int]:
    """Generate NPC/hidden damage dice on the runtime path (never for PCs)."""
    if type(num_dice) is not int or not 1 <= num_dice <= 100:
        raise AttackError(
            "invalid_dice_count",
            f"can generate 1-100 damage dice, got {num_dice}",
            field="damage_rolls",
        )
    roller = rng if rng is not None else secrets.SystemRandom()
    try:
        return [int(roller.randint(1, die_size)) for _ in range(num_dice)]
    except Exception as exc:
        raise AttackError(
            "dice_generation_failed",
            f"runtime damage dice generation failed: {exc}",
            field="damage_rolls",
        ) from exc


def runtime_d20(count: int, *, rng: Any | None = None) -> list[int]:
    """Generate NPC/hidden d20 results on the runtime path (never for PCs)."""
    if count not in (1, 2):
        raise AttackError(
            "invalid_dice_count",
            f"can generate 1 or 2 d20 results, got {count}",
            field="dice",
        )
    roller = rng if rng is not None else secrets.SystemRandom()
    try:
        return [int(roller.randint(1, 20)) for _ in range(count)]
    except Exception as exc:
        raise AttackError(
            "dice_generation_failed",
            f"runtime dice generation failed: {exc}",
            field="dice",
        ) from exc


# ── Result DTOs ───────────────────────────────────────────────────────────


class AttackResolution(StrictModel):
    attack_id: str
    attacker_name: str = "Unnamed Attack"
    attacker_kind: AttackerKind = "pc"
    advantage_state: AdvantageState = "normal"
    advantage_sources: list[AdvantageSource] = Field(default_factory=list)
    dice_all: list[int] = Field(default_factory=list)
    die_kept: int = 0
    dice_dropped: list[int] = Field(default_factory=list)
    die_source: DieSource = "player_supplied"
    die_visibility: DieVisibility = "public"
    attack_bonus: int = 0
    attack_bonus_components: dict[str, int] = Field(default_factory=dict)
    calculation_path: str = ""
    total: int = 0
    armor_class: int | None = None
    ac_visibility: ACVisibility = "hidden"
    outcome: AttackOutcome = "miss"
    is_critical: bool = False
    is_natural_20: bool = False
    is_natural_1: bool = False
    crit_threshold: int = 20
    status: Literal["resolved"] = "resolved"
    provenance: dict[str, Any] = Field(default_factory=dict)

    def public_projection(self) -> dict[str, Any]:
        """Observable outcome: hidden AC/dice stay DM-only unless public."""
        value: dict[str, Any] = {
            "attack_id": self.attack_id,
            "attacker_name": self.attacker_name,
            "advantage_state": self.advantage_state,
            "outcome": self.outcome,
            "is_critical": self.is_critical,
            "status": self.status,
        }
        if self.die_visibility == "public":
            value.update(
                die_kept=self.die_kept,
                dice_all=list(self.dice_all),
                total=self.total,
                attack_bonus=self.attack_bonus,
                is_natural_20=self.is_natural_20,
                is_natural_1=self.is_natural_1,
            )
        if self.ac_visibility == "public":
            value["armor_class"] = self.armor_class
        return value

    def to_event_payload(self, *, include_private: bool) -> dict[str, Any]:
        if include_private:
            return self.model_dump(mode="json")
        return self.public_projection()


class DamageResolution(StrictModel):
    damage_id: str
    attack_id: str | None = None
    damage_type: str = "untyped"
    is_critical: bool = False
    dice_expected: int = 0
    dice_rolled: list[int] = Field(default_factory=list)
    die_source: DieSource = "player_supplied"
    die_visibility: DieVisibility = "public"
    modifier: int = 0
    damage_components: dict[str, int] = Field(default_factory=dict)
    pre_mitigation: int = 0
    mitigation: str = "none"  # none | resistance | vulnerability | immunity | resistance+vulnerability
    final_total: int = 0
    status: Literal["resolved"] = "resolved"
    provenance: dict[str, Any] = Field(default_factory=dict)

    def public_projection(self) -> dict[str, Any]:
        # The explicit mitigation classification (resistance/vulnerability/
        # immunity) is a hidden NPC stat and is never public — narration may
        # convey the fictional effect, but the mechanical label stays
        # DM-private. It remains in the private payload for audit.
        value: dict[str, Any] = {
            "damage_id": self.damage_id,
            "damage_type": self.damage_type,
            "is_critical": self.is_critical,
            "final_total": self.final_total,
            "status": self.status,
        }
        if self.die_visibility == "public":
            value.update(
                dice_rolled=list(self.dice_rolled),
                pre_mitigation=self.pre_mitigation,
                modifier=self.modifier,
            )
        return value

    def to_event_payload(self, *, include_private: bool) -> dict[str, Any]:
        if include_private:
            return self.model_dump(mode="json")
        return self.public_projection()


class HPChange(StrictModel):
    """Pure HP delta record: before/after snapshots plus labeled amounts."""

    change_id: str
    kind: Literal["damage", "temporary_grant", "heal"] = "damage"
    before: HitPoints = Field(
        default_factory=lambda: HitPoints(current=0, maximum=1, temporary=0)
    )
    after: HitPoints = Field(
        default_factory=lambda: HitPoints(current=0, maximum=1, temporary=0)
    )
    absorbed_by_temp: int = 0
    applied_to_current: int = 0
    restored_to_current: int = 0
    temp_granted: int = 0
    is_down: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)

    def public_projection(self) -> dict[str, Any]:
        """Observable outcome only: exact HP snapshots stay DM-private.

        Before/after current/maximum/temporary values can be hidden NPC
        stats, so the public form carries just the change kind and whether
        the target dropped — both observable at a shared table — and never
        HP numbers.
        """
        return {
            "change_id": self.change_id,
            "kind": self.kind,
            "is_down": self.is_down,
        }


# ── Core resolution ───────────────────────────────────────────────────────


def _require_stable_id(value: str | None, *, field: str, what: str) -> str:
    """Validate a caller-supplied stable logical ID (never minted here)."""
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise AttackError(
            "missing_roll_id"
            if field in ("attack_id", "damage_id")
            else "missing_effect_id",
            f"{what} is required: supply one stable ID per logical {what} and reuse it on retry",
            field=field,
        )
    return value


def _validate_advantage_state(value: str) -> AdvantageState:
    if value not in ("normal", "advantage", "disadvantage"):
        raise AttackError(
            "invalid_advantage_state",
            f"advantage_state must be normal/advantage/disadvantage, got {value!r}",
            field="advantage_state",
        )
    return value  # type: ignore[return-value]


def _validate_crit_threshold(value: int) -> int:
    if type(value) is not int or not 2 <= value <= 20:
        raise AttackError(
            "invalid_crit_threshold",
            f"crit_threshold must be an integer 2-20, got {value!r}",
            field="crit_threshold",
        )
    return value


def _derived_id(base: str, suffix: str) -> str:
    """Stable derived ID that stays within the 128-char bound."""
    candidate = f"{base}:{suffix}"
    if len(candidate) <= 128:
        return candidate
    import hashlib

    return f"atk:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


def resolve_attack_roll(
    *,
    attacker: CombatantOffense,
    defender: CombatantDefense,
    attacker_kind: AttackerKind = "pc",
    dice: list[int] | None = None,
    advantage_state: AdvantageState | None = None,
    advantage_sources: list[AdvantageSource] | None = None,
    situational_bonus: int = 0,
    ac_visibility: ACVisibility = "hidden",
    die_visibility: DieVisibility | None = None,
    crit_threshold: int = 20,
    attack_id: str | None = None,
    rng: Any | None = None,
) -> AttackResolution:
    """Deterministically resolve one attack roll against an AC. Pure.

    - ``attacker_kind="pc"``: ``dice`` must be player-supplied (never
      generated). ``attacker_kind="npc"``: DM-supplied or runtime-generated.
    - Natural 20 (or ``crit_threshold``+) always critically hits; natural 1
      always misses; otherwise ``total >= AC`` hits (2024 meet-or-beat).
    - ``attack_id`` is required: the caller mints one stable ID per logical
      attack on first attempt and reuses it on every retry.
    """
    t0 = time.monotonic()
    aid = _require_stable_id(attack_id, field="attack_id", what="attack_id")
    invalid_input: str | None = None
    calculation_path = attacker.calculation_path
    logged_state: str = advantage_state or "normal"
    try:
        if attacker_kind not in ("pc", "npc"):
            raise AttackError(
                "invalid_roller",
                f"attacker_kind must be pc/npc, got {attacker_kind!r}",
                field="attacker_kind",
            )
        if type(situational_bonus) is not int:
            raise AttackError(
                "invalid_situational_bonus",
                "situational_bonus must be int",
                field="situational_bonus",
            )
        threshold = _validate_crit_threshold(crit_threshold)

        sources = list(advantage_sources or [])
        for source in sources:
            if source not in ("advantage", "disadvantage"):
                raise AttackError(
                    "invalid_advantage_source",
                    f"unknown advantage source {source!r}",
                    field="advantage_sources",
                )
        state = (
            combine_advantage(sources)
            if sources
            else _validate_advantage_state(advantage_state or "normal")
        )
        logged_state = state

        expected_count = 2 if state in ("advantage", "disadvantage") else 1
        if dice is None:
            if attacker_kind == "pc":
                raise AttackError(
                    "missing_player_die",
                    "PC attack dice must be supplied by the player; this service never generates them",
                    field="dice",
                )
            dice = runtime_d20(expected_count, rng=rng)
            die_source: DieSource = "runtime_generated"
        else:
            die_source = "player_supplied" if attacker_kind == "pc" else "dm_supplied"

        try:
            kept, dropped = select_d20(list(dice), state)  # type: ignore[arg-type]
        except ValueError as exc:
            code = getattr(exc, "code", "invalid_dice")
            raise AttackError(
                code, str(exc), field=getattr(exc, "field", "dice")
            ) from exc

        total = kept + attacker.attack_bonus + situational_bonus
        is_nat20 = kept == 20
        is_nat1 = kept == 1
        is_crit_range = kept >= threshold

        # 2024 attack outcomes: nat 1 always misses, crit-range always hits
        # critically, otherwise meet-or-beat the authoritative AC.
        if is_nat1:
            outcome: AttackOutcome = "miss"
            is_critical = False
        elif is_nat20 or is_crit_range:
            outcome = "critical"
            is_critical = True
        elif total >= defender.armor_class:
            outcome = "hit"
            is_critical = False
        else:
            outcome = "miss"
            is_critical = False

        visibility: DieVisibility = die_visibility or (
            "public" if attacker_kind == "pc" else "hidden"
        )
        if visibility not in ("public", "hidden"):
            raise AttackError(
                "invalid_die_visibility",
                f"die_visibility must be public/hidden, got {visibility!r}",
                field="die_visibility",
            )
        if ac_visibility not in ("public", "hidden"):
            raise AttackError(
                "invalid_ac_visibility",
                f"ac_visibility must be public/hidden, got {ac_visibility!r}",
                field="ac_visibility",
            )

        components: dict[str, int] = {"attack_bonus": attacker.attack_bonus}
        if situational_bonus:
            components["situational_bonus"] = situational_bonus

        result = AttackResolution(
            attack_id=aid,
            attacker_name=attacker.attack_name,
            attacker_kind=attacker_kind,
            advantage_state=state,
            advantage_sources=sources,
            dice_all=list(dice),
            die_kept=kept,
            dice_dropped=dropped,
            die_source=die_source,
            die_visibility=visibility,
            attack_bonus=attacker.attack_bonus + situational_bonus,
            attack_bonus_components=components,
            calculation_path=calculation_path,
            total=total,
            armor_class=defender.armor_class,
            ac_visibility=ac_visibility,
            outcome=outcome,
            is_critical=is_critical,
            is_natural_20=is_nat20,
            is_natural_1=is_nat1,
            crit_threshold=threshold,
            provenance={
                **attacker.provenance,
                "calculation_path": calculation_path,
                "defense_path": defender.calculation_path,
                "mechanics_version": MECHANICS_VERSION,
                "rules_revision": RULES_REVISION,
                "attack_version": ATTACK_VERSION,
                "die_source": die_source,
                "attacker_kind": attacker_kind,
                "situational_bonus": situational_bonus,
            },
        )
    except AttackError as exc:
        invalid_input = exc.code
        raise
    finally:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        try:
            structured_log(
                logger,
                logging.INFO,
                "attack_resolution",
                attack_id=aid,
                outcome=locals().get("result", None).outcome
                if "result" in locals()
                else None,
                calculation_path=calculation_path,
                advantage_state=logged_state,
                invalid_input=invalid_input,
                latency_ms=latency_ms,
                has_hidden_ac=bool(ac_visibility == "hidden"),
                ac_visibility=ac_visibility,
                die_source=(
                    "player_supplied"
                    if attacker_kind == "pc" and dice is not None
                    else ("runtime" if attacker_kind == "npc" else "missing")
                ),
                attacker_kind=attacker_kind,
                attack_version=ATTACK_VERSION,
            )
        except Exception:
            pass
    return result


def resolve_damage(
    *,
    spec: DamageSpec,
    damage_rolls: list[int] | None = None,
    attacker_kind: AttackerKind = "pc",
    is_critical: bool = False,
    defender: CombatantDefense | None = None,
    extra_modifiers: list[DamageContribution] | None = None,
    hooks: list[DamageHook | Callable[[DamageHookContext], int]] | None = None,
    die_visibility: DieVisibility | None = None,
    damage_id: str | None = None,
    attack_id: str | None = None,
    rng: Any | None = None,
) -> DamageResolution:
    """Deterministically resolve damage from supplied/generated dice. Pure.

    Critical hits double the damage dice (modifiers added once); the
    supplied/generated roll count must match the expected count exactly.
    Resistances halve (round down), vulnerabilities double, immunities
    negate; resistance + vulnerability both apply. ``attacker_kind="pc"``
    requires player-supplied dice.
    """
    t0 = time.monotonic()
    did = _require_stable_id(damage_id, field="damage_id", what="damage_id")
    invalid_input: str | None = None
    try:
        if attacker_kind not in ("pc", "npc"):
            raise AttackError(
                "invalid_roller",
                f"attacker_kind must be pc/npc, got {attacker_kind!r}",
                field="attacker_kind",
            )
        if type(is_critical) is not bool:
            raise AttackError(
                "invalid_critical", "is_critical must be bool", field="is_critical"
            )

        expected = spec.num_dice * (2 if is_critical else 1)
        if damage_rolls is None:
            if attacker_kind == "pc":
                raise AttackError(
                    "missing_player_die",
                    "PC damage dice must be supplied by the player; this service never generates them",
                    field="damage_rolls",
                )
            damage_rolls = runtime_damage_dice(
                num_dice=expected, die_size=spec.die_size, rng=rng
            )
            die_source: DieSource = "runtime_generated"
        else:
            die_source = "player_supplied" if attacker_kind == "pc" else "dm_supplied"
            if len(damage_rolls) != expected:
                raise AttackError(
                    "invalid_dice_count",
                    f"{'critical' if is_critical else 'normal'} damage expects exactly {expected} d{spec.die_size} result(s), got {len(damage_rolls)}",
                    field="damage_rolls",
                    details={"expected": expected, "count": len(damage_rolls)},
                )
        for face in damage_rolls:
            if type(face) is not int or not 1 <= face <= spec.die_size:
                raise AttackError(
                    "invalid_die_value",
                    f"damage dice must be integers 1-{spec.die_size}, got {face!r}",
                    field="damage_rolls",
                )

        extras = list(extra_modifiers or [])
        for extra in extras:
            if type(extra.value) is not int:
                raise AttackError(
                    "invalid_extra_modifier",
                    f"extra modifier {extra.name!r} must be int",
                    field="extra_modifiers",
                )

        hook_context = DamageHookContext(
            damage_type=spec.damage_type,
            is_critical=is_critical,
            base_damage=sum(damage_rolls),
        )
        hook_contribs: list[DamageContribution] = []
        for idx, hook in enumerate(hooks or []):
            contrib = _hook_bonus(hook, hook_context, name=f"hook_{idx}")
            if contrib.value:
                hook_contribs.append(contrib)

        pre = sum(damage_rolls) + spec.modifier
        components: dict[str, int] = {
            "dice_sum": sum(damage_rolls),
            "modifier": spec.modifier,
        }
        for extra in extras:
            if extra.value:
                components[f"extra:{extra.name}"] = extra.value
                pre += extra.value
        for contrib in hook_contribs:
            components[f"hook:{contrib.name}"] = contrib.value
            pre += contrib.value
        pre = max(0, pre)

        dtype = spec.damage_type
        resisted = defender is not None and dtype in defender.resistances
        vulnerable = defender is not None and dtype in defender.vulnerabilities
        immune = defender is not None and dtype in defender.immunities
        if immune:
            mitigation = "immunity"
            final = 0
        elif resisted and vulnerable:
            mitigation = "resistance+vulnerability"
            final = (pre // 2) * 2
        elif resisted:
            mitigation = "resistance"
            final = pre // 2
        elif vulnerable:
            mitigation = "vulnerability"
            final = pre * 2
        else:
            mitigation = "none"
            final = pre

        visibility: DieVisibility = die_visibility or (
            "public" if attacker_kind == "pc" else "hidden"
        )
        if visibility not in ("public", "hidden"):
            raise AttackError(
                "invalid_die_visibility",
                f"die_visibility must be public/hidden, got {visibility!r}",
                field="die_visibility",
            )

        result = DamageResolution(
            damage_id=did,
            attack_id=attack_id,
            damage_type=dtype,
            is_critical=is_critical,
            dice_expected=expected,
            dice_rolled=list(damage_rolls),
            die_source=die_source,
            die_visibility=visibility,
            modifier=spec.modifier,
            damage_components=components,
            pre_mitigation=pre,
            mitigation=mitigation,
            final_total=final,
            provenance={
                "calculation_path": "attack_authoritative"
                if attacker_kind == "pc"
                else "dm_supplied",
                "mechanics_version": MECHANICS_VERSION,
                "rules_revision": RULES_REVISION,
                "attack_version": ATTACK_VERSION,
                "die_source": die_source,
                "attacker_kind": attacker_kind,
                "damage_expression": f"{spec.num_dice}d{spec.die_size}{spec.modifier:+d}"
                if spec.modifier
                else f"{spec.num_dice}d{spec.die_size}",
                "hook_names": [c.name for c in hook_contribs],
            },
        )
    except AttackError as exc:
        invalid_input = exc.code
        raise
    finally:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        try:
            structured_log(
                logger,
                logging.INFO,
                "damage_resolution",
                damage_id=did,
                attack_id=attack_id,
                mitigation=locals().get("result", None).mitigation
                if "result" in locals()
                else None,
                invalid_input=invalid_input,
                latency_ms=latency_ms,
                die_source=(
                    "player_supplied"
                    if attacker_kind == "pc" and damage_rolls is not None
                    else ("runtime" if attacker_kind == "npc" else "missing")
                ),
                attacker_kind=attacker_kind,
                attack_version=ATTACK_VERSION,
            )
        except Exception:
            pass
    return result


# ── HP / temp-HP primitives (pure) ────────────────────────────────────────


def apply_damage(hp: HitPoints, amount: int, *, change_id: str) -> HPChange:
    """Apply damage through temp HP first, then current HP (floor 0). Pure."""
    cid = _require_stable_id(change_id, field="change_id", what="change_id")
    if type(amount) is not int or amount < 0:
        raise AttackError(
            "invalid_damage_amount",
            f"damage amount must be an integer >= 0, got {amount!r}",
            field="amount",
        )
    absorbed = min(hp.temporary, amount)
    remainder = amount - absorbed
    applied = min(hp.current, remainder)
    after = HitPoints(
        current=hp.current - applied,
        maximum=hp.maximum,
        temporary=hp.temporary - absorbed,
    )
    return HPChange(
        change_id=cid,
        kind="damage",
        before=hp,
        after=after,
        absorbed_by_temp=absorbed,
        applied_to_current=applied,
        is_down=after.current == 0,
        provenance={"attack_version": ATTACK_VERSION, "rules_revision": RULES_REVISION},
    )


def grant_temporary_hp(hp: HitPoints, amount: int, *, change_id: str) -> HPChange:
    """Grant temp HP: never stacks — the higher value wins. Pure.

    2024 lets the creature keep current or take new temp HP; the
    deterministic code-owned choice is the higher value, so retries and
    concurrent grants converge instead of depending on call order.
    """
    cid = _require_stable_id(change_id, field="change_id", what="change_id")
    if type(amount) is not int or amount < 0:
        raise AttackError(
            "invalid_temp_hp_amount",
            f"temp HP amount must be an integer >= 0, got {amount!r}",
            field="amount",
        )
    granted = max(0, amount - hp.temporary)
    after = HitPoints(
        current=hp.current, maximum=hp.maximum, temporary=max(hp.temporary, amount)
    )
    return HPChange(
        change_id=cid,
        kind="temporary_grant",
        before=hp,
        after=after,
        temp_granted=granted,
        is_down=after.current == 0,
        provenance={"attack_version": ATTACK_VERSION, "rules_revision": RULES_REVISION},
    )


def heal_damage(hp: HitPoints, amount: int, *, change_id: str) -> HPChange:
    """Restore current HP, capped at maximum. Temp HP untouched. Pure."""
    cid = _require_stable_id(change_id, field="change_id", what="change_id")
    if type(amount) is not int or amount < 0:
        raise AttackError(
            "invalid_heal_amount",
            f"heal amount must be an integer >= 0, got {amount!r}",
            field="amount",
        )
    restored = min(hp.maximum - hp.current, amount)
    after = HitPoints(
        current=hp.current + restored, maximum=hp.maximum, temporary=hp.temporary
    )
    return HPChange(
        change_id=cid,
        kind="heal",
        before=hp,
        after=after,
        restored_to_current=restored,
        is_down=after.current == 0,
        provenance={"attack_version": ATTACK_VERSION, "rules_revision": RULES_REVISION},
    )


# ── Full attack flow (pure composition) ───────────────────────────────────


class FullAttackResult(StrictModel):
    attack: AttackResolution
    damage: DamageResolution | None = None
    hp_change: HPChange | None = None

    def to_event_payload(self, *, include_private: bool) -> dict[str, Any]:
        return {
            "attack": self.attack.to_event_payload(include_private=include_private),
            "damage": self.damage.to_event_payload(include_private=include_private)
            if self.damage
            else None,
            "hp_change": self.hp_change.public_projection() if self.hp_change else None,
        }


def resolve_full_attack(
    *,
    attacker: CombatantOffense,
    defender: CombatantDefense,
    defender_hp: HitPoints | None = None,
    attacker_kind: AttackerKind = "pc",
    attack_dice: list[int] | None = None,
    damage_spec: DamageSpec | None = None,
    damage_rolls: list[int] | None = None,
    advantage_state: AdvantageState | None = None,
    advantage_sources: list[AdvantageSource] | None = None,
    situational_bonus: int = 0,
    extra_modifiers: list[DamageContribution] | None = None,
    hooks: list[DamageHook | Callable[[DamageHookContext], int]] | None = None,
    ac_visibility: ACVisibility = "hidden",
    die_visibility: DieVisibility | None = None,
    crit_threshold: int = 20,
    attack_id: str | None = None,
    damage_id: str | None = None,
    change_id: str | None = None,
    rng: Any | None = None,
) -> FullAttackResult:
    """Resolve attack roll, then damage + HP application on a hit. Pure.

    On a miss no damage is resolved (``damage``/``hp_change`` stay None) —
    callers must not invent damage for a missed attack. On a hit without a
    ``damage_spec`` the attack resolves but damage stays unresolved rather
    than guessed. HP application requires an explicit ``defender_hp``
    snapshot, otherwise damage resolves without an ``hp_change``.
    """
    attack = resolve_attack_roll(
        attacker=attacker,
        defender=defender,
        attacker_kind=attacker_kind,
        dice=attack_dice,
        advantage_state=advantage_state,
        advantage_sources=advantage_sources,
        situational_bonus=situational_bonus,
        ac_visibility=ac_visibility,
        die_visibility=die_visibility,
        crit_threshold=crit_threshold,
        attack_id=attack_id,
        rng=rng,
    )
    if attack.outcome == "miss" or damage_spec is None:
        return FullAttackResult(attack=attack)
    damage = resolve_damage(
        spec=damage_spec,
        damage_rolls=damage_rolls,
        attacker_kind=attacker_kind,
        is_critical=attack.is_critical,
        defender=defender,
        extra_modifiers=extra_modifiers,
        hooks=hooks,
        die_visibility=die_visibility,
        damage_id=damage_id
        if damage_id is not None
        else _derived_id(attack.attack_id, "damage"),
        attack_id=attack.attack_id,
        rng=rng,
    )
    hp_change: HPChange | None = None
    if defender_hp is not None:
        hp_change = apply_damage(
            defender_hp,
            damage.final_total,
            change_id=change_id
            if change_id is not None
            else _derived_id(damage.damage_id, "hp"),
        )
    return FullAttackResult(attack=attack, damage=damage, hp_change=hp_change)


# ── Staged-effect + domain-event records ──────────────────────────────────


def build_damage_effect(
    *,
    effect_id: str,
    target_kind: Literal["pc", "npc"],
    target_id: str,
    damage: DamageResolution,
    attack: AttackResolution | None = None,
    visibility: str = "dm_private",
) -> dict[str, Any]:
    """Typed staged-effect record for the #206 effect registry.

    Carries the already-resolved damage total — promotion applies code-owned
    HP arithmetic, never model output. Defaults to ``dm_private`` so
    promotion from a private attempt cannot broaden disclosure; callers
    widen explicitly for shared-table damage.
    """
    _require_stable_id(effect_id, field="effect_id", what="effect_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", effect_id) or len(effect_id) > 48:
        raise AttackError(
            "invalid_effect_id",
            "effect_id must match [A-Za-z0-9_-]+ (1-48 chars) so the effect survives canonical DM-contract validation",
            field="effect_id",
        )
    if target_kind not in ("pc", "npc"):
        raise AttackError(
            "invalid_target",
            f"target_kind must be pc/npc, got {target_kind!r}",
            field="target_kind",
        )
    if visibility not in (
        "dm_private",
        "party_known",
        "public",
        "dm_only",
        "campaign",
        "private",
    ):
        raise AttackError(
            "invalid_visibility",
            f"unknown visibility {visibility!r}",
            field="visibility",
        )
    return {
        "id": effect_id,
        "effect_type": "apply_attack_damage",
        "arguments": {
            "target_kind": target_kind,
            "target_id": str(target_id),
            "damage_total": damage.final_total,
            "damage_type": damage.damage_type,
            "mitigation": damage.mitigation,
            "damage_id": damage.damage_id,
            "attack_id": attack.attack_id if attack else damage.attack_id,
            "visibility": visibility,
        },
    }


def attack_domain_event(
    attack: AttackResolution, *, include_private: bool
) -> tuple[str, dict[str, Any], str]:
    """(event_type, payload, visibility) triple for commit_campaign_mutation."""
    return (
        ATTACK_RESOLVED_EVENT,
        attack.to_event_payload(include_private=include_private),
        "dm_private" if attack.ac_visibility == "hidden" else "public",
    )


def damage_domain_event(
    damage: DamageResolution,
    hp_change: HPChange | None,
    *,
    include_private: bool,
    visibility: str = "public",
) -> tuple[str, dict[str, Any], str]:
    """(event_type, payload, visibility) triple for commit_campaign_mutation."""
    payload = damage.to_event_payload(include_private=include_private)
    if hp_change is None:
        payload["hp_change"] = None
    elif include_private:
        payload["hp_change"] = hp_change.model_dump(mode="json")
    else:
        payload["hp_change"] = hp_change.public_projection()
    return (DAMAGE_APPLIED_EVENT, payload, visibility)


# ── Duplicate-application guard ───────────────────────────────────────────


class AttackLedger:
    """Process-local guard so one logical attack cannot apply twice in-process.

    Resolution itself is pure (safe to retry); register the ``attack_id``
    here when its consequence is applied. Returns ``"applied"`` on first use
    and ``"duplicate"`` thereafter. Same scope warning as #225's
    ``ResolutionLedger``: in-memory only — for crash-safe / multi-worker
    idempotency use :func:`apply_attack_consequence`.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def apply(self, attack_id: str) -> Literal["applied", "duplicate"]:
        if attack_id in self._applied:
            return "duplicate"
        self._applied.add(attack_id)
        return "applied"

    def is_duplicate(self, attack_id: str) -> bool:
        return attack_id in self._applied

    def __len__(self) -> int:
        return len(self._applied)


def apply_attack_consequence(
    db: Any,
    *,
    actor_id: Any,
    scope_type: str,
    scope_id: Any,
    attack: AttackResolution,
    damage: DamageResolution | None = None,
    hp_change: HPChange | None = None,
    execute: Callable[[], dict | list],
    command_type: str = "attack.apply",
) -> tuple[dict | list, bool]:
    """Durably apply one logical attack's consequence exactly once.

    Keyed by the stable ``attack.attack_id`` (caller-minted on first
    attempt, reused on retry) via the existing ``app.idempotency`` durable
    command record — no new persistence mechanism. A retry that resolves to
    materially different attack/damage payload raises
    ``IdempotencyConflictError`` rather than double-applying.

    Returns ``(result, replayed)`` where ``replayed`` is True on dedup hits.
    """
    from app.idempotency import execute_idempotent_command

    return execute_idempotent_command(
        db,
        actor_id=actor_id,
        idempotency_key=attack.attack_id,
        command_type=command_type,
        scope_type=scope_type,
        scope_id=scope_id,
        payload={
            "attack": attack.model_dump(mode="json"),
            "damage": damage.model_dump(mode="json") if damage else None,
            "hp_change": hp_change.model_dump(mode="json") if hp_change else None,
        },
        execute=execute,
    )
