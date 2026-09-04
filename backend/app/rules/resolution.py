"""Deterministic d20 check/save resolution primitives — issue #225.

Pure calculation over authoritative mechanical inputs (#224) and explicit dice.
The DM decides whether a roll is required; this module resolves it without
asking the LLM to perform arithmetic.

Invariants:
- Totals are deterministic from authoritative inputs (sheet-derived modifiers
  via :mod:`app.rules.mechanics`, or explicit DM-supplied modifiers for NPCs).
- Player PC dice are *supplied*, never generated here. NPC/hidden dice are
  generated via the runtime path (injectable RNG) or DM-supplied.
- Advantage/disadvantage follow 2024 core: they never stack, and any source
  of each cancels to a straight roll.
- 2024 D20 Tests: natural 20 auto-succeeds, natural 1 auto-fails (vs a DC).
- Hidden DCs never appear in public projections or player telemetry.
- Resolution is pure — safe to retry. Duplicate *application* of one logical
  roll is guarded by :class:`ResolutionLedger`.
- Missing inputs produce :class:`ResolutionError`, never guessed modifiers.

Out of scope: attacks/damage (#226).
"""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from typing import Any, Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.observability.tracing import structured_log
from app.rules.mechanics import (
    MECHANICS_VERSION,
    RULES_REVISION,
    MechanicsError,
    get_character_mechanics_for_sheet,
)

logger = logging.getLogger(__name__)

RESOLUTION_VERSION = "resolution_v1"

RollKind = Literal["check", "save"]
AdvantageState = Literal["normal", "advantage", "disadvantage"]
AdvantageSource = Literal["advantage", "disadvantage"]
DieSource = Literal["player_supplied", "dm_supplied", "runtime_generated"]
DCVisibility = Literal["public", "hidden"]
DieVisibility = Literal["public", "hidden"]
ResolutionStatus = Literal["resolved", "unresolved"]

CALCULATION_PATHS = {
    "skill_authoritative",
    "save_authoritative",
    "ability_authoritative",
    "dm_supplied",
}


class ResolutionError(ValueError):
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


# ── Advantage ─────────────────────────────────────────────────────────────


def combine_advantage(sources: list[AdvantageSource]) -> AdvantageState:
    """Cancel advantage/disadvantage sources per 2024 core play.

    Multiple sources never stack: any advantage + any disadvantage from any
    number of sources cancels to a straight (normal) roll.
    """
    has_adv = "advantage" in sources
    has_dis = "disadvantage" in sources
    if has_adv and has_dis:
        return "normal"
    if has_adv:
        return "advantage"
    if has_dis:
        return "disadvantage"
    return "normal"


def _validate_advantage_state(value: str) -> AdvantageState:
    if value not in ("normal", "advantage", "disadvantage"):
        raise ResolutionError(
            "invalid_advantage_state",
            f"advantage_state must be normal/advantage/disadvantage, got {value!r}",
            field="advantage_state",
        )
    return value  # type: ignore[return-value]


def select_d20(dice: list[int], advantage_state: AdvantageState) -> tuple[int, list[int]]:
    """Return (kept, dropped) for validated d20 faces under an advantage state."""
    expected = 2 if advantage_state in ("advantage", "disadvantage") else 1
    if len(dice) != expected:
        raise ResolutionError(
            "invalid_dice_count",
            f"{advantage_state} requires exactly {expected} d20 result(s), got {len(dice)}",
            field="dice",
            details={"advantage_state": advantage_state, "count": len(dice)},
        )
    for die in dice:
        if type(die) is not int or not 1 <= die <= 20:
            raise ResolutionError(
                "invalid_die_value",
                f"d20 results must be integers 1-20, got {die!r}",
                field="dice",
            )
    if advantage_state == "advantage":
        kept = max(dice)
    elif advantage_state == "disadvantage":
        kept = min(dice)
    else:
        kept = dice[0]
    dropped = [d for d in dice]
    dropped.remove(kept)  # removes one instance on doubles
    return kept, dropped


# ── Modifier extension hooks ──────────────────────────────────────────────


class HookContext(StrictModel):
    kind: RollKind
    target: str  # skill key or ability name
    ability: str | None = None
    base_modifier: int = 0


class ModifierContribution(StrictModel):
    name: str
    value: int
    source: str = "situational"


class ModifierHook(Protocol):
    def bonus(self, context: HookContext) -> int: ...


def _hook_bonus(hook: ModifierHook | Callable[[HookContext], int], context: HookContext, name: str) -> ModifierContribution:
    try:
        if hasattr(hook, "bonus"):
            value = hook.bonus(context)  # type: ignore[union-attr]
        else:
            value = hook(context)  # type: ignore[operator]
    except ResolutionError:
        raise
    except Exception as exc:
        raise ResolutionError(
            "hook_failed",
            f"modifier hook {name!r} failed: {exc}",
            field="hooks",
            details={"hook": name},
        ) from exc
    if type(value) is not int:
        raise ResolutionError(
            "hook_failed",
            f"modifier hook {name!r} must return int, got {type(value).__name__}",
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
    return ModifierContribution(name=resolved_name, value=value, source="hook")


class ResolvedModifier(StrictModel):
    modifier: int
    calculation_path: str
    components: dict[str, int] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)


def resolve_modifier(
    sheet: Any | None,
    *,
    kind: RollKind,
    skill: str | None = None,
    ability: str | None = None,
    modifier: int | None = None,
    situational_bonus: int = 0,
    extra_modifiers: list[ModifierContribution] | None = None,
    hooks: list[ModifierHook | Callable[[HookContext], int]] | None = None,
) -> ResolvedModifier:
    """Resolve the total modifier from authoritative inputs.

    Either a character ``sheet`` (authoritative #224 derivation) or an
    explicit DM-supplied ``modifier`` (NPCs / sheet-less entities) is
    required — never both missing, never silently defaulted.
    """
    if kind not in ("check", "save"):
        raise ResolutionError("invalid_roll_kind", f"kind must be check/save, got {kind!r}", field="kind")
    if type(situational_bonus) is not int:
        raise ResolutionError("invalid_situational_bonus", "situational_bonus must be int", field="situational_bonus")

    extras = list(extra_modifiers or [])
    for extra in extras:
        if type(extra.value) is not int:
            raise ResolutionError("invalid_extra_modifier", f"extra modifier {extra.name!r} must be int", field="extra_modifiers")

    base = 0
    components: dict[str, int] = {}
    provenance: dict[str, Any] = {}
    path = "dm_supplied"

    if sheet is not None:
        if kind == "save":
            if not ability:
                raise ResolutionError("missing_target", "save resolution requires ability", field="ability")
            try:
                detail = get_character_mechanics_for_sheet(sheet).saves[_norm_ability_or_raise(ability)]
            except MechanicsError as exc:
                raise ResolutionError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
            base = detail.effective
            components = dict(detail.components)
            if detail.override_active:
                components["override_effective"] = detail.effective
            path = "save_authoritative"
            provenance = {"target": detail.ability, "proficient": detail.proficient, "modifier_source": detail.source}
        elif skill:
            try:
                mechanics = get_character_mechanics_for_sheet(sheet)
                from app.rules.mechanics import _norm_skill_name

                norm = _norm_skill_name(skill)
                if norm is None:
                    raise ResolutionError("unknown_skill", f"unknown skill {skill!r}", field="skill")
                detail = mechanics.skills[norm]
            except MechanicsError as exc:
                raise ResolutionError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
            base = detail.effective
            components = dict(detail.components)
            if detail.override_active:
                components["override_effective"] = detail.effective
            path = "skill_authoritative"
            provenance = {
                "target": detail.skill,
                "ability": detail.ability,
                "proficient": detail.proficient,
                "expertise": detail.expertise,
                "modifier_source": detail.source,
            }
        elif ability:
            try:
                mechanics = get_character_mechanics_for_sheet(sheet)
                norm_ability = _norm_ability_or_raise(ability)
                base = mechanics.abilities[norm_ability].modifier
            except MechanicsError as exc:
                raise ResolutionError(exc.code, str(exc), field=exc.field, details=dict(exc.details)) from exc
            components = {"ability_mod": base}
            path = "ability_authoritative"
            provenance = {"target": norm_ability}
        else:
            raise ResolutionError("missing_target", "check resolution requires skill or ability", field="skill")
        if modifier is not None:
            raise ResolutionError(
                "conflicting_modifier_inputs",
                "pass either a sheet or an explicit modifier, not both",
                field="modifier",
            )
    else:
        if modifier is None:
            raise ResolutionError(
                "missing_mechanical_input",
                "no sheet and no explicit modifier: cannot derive an authoritative modifier",
                field="modifier",
            )
        if type(modifier) is not int:
            raise ResolutionError("invalid_modifier", "explicit modifier must be int", field="modifier")
        base = modifier
        components = {"dm_supplied": modifier}
        provenance = {"target": skill or ability or "unspecified"}

    hook_context = HookContext(
        kind=kind,
        target=str(provenance.get("target") or skill or ability or ""),
        ability=str(provenance.get("ability") or ability or "") or None,
        base_modifier=base,
    )
    hook_contribs: list[ModifierContribution] = []
    for idx, hook in enumerate(hooks or []):
        contrib = _hook_bonus(hook, hook_context, name=f"hook_{idx}")
        if contrib.value:
            hook_contribs.append(contrib)

    total = base + situational_bonus
    merged_components = dict(components)
    if situational_bonus:
        merged_components["situational_bonus"] = situational_bonus
    for extra in extras:
        if extra.value:
            merged_components[f"extra:{extra.name}"] = extra.value
            total += extra.value
    for contrib in hook_contribs:
        merged_components[f"hook:{contrib.name}"] = contrib.value
        total += contrib.value

    provenance.update({
        "calculation_path": path,
        "mechanics_version": MECHANICS_VERSION,
        "rules_revision": RULES_REVISION,
        "situational_bonus": situational_bonus,
        "hook_names": [c.name for c in hook_contribs],
    })
    return ResolvedModifier(modifier=total, calculation_path=path, components=merged_components, provenance=provenance)


def _norm_ability_or_raise(raw: str) -> str:
    from app.rules.mechanics import _norm_ability

    norm = _norm_ability(raw)
    if norm is None:
        raise ResolutionError("unknown_ability", f"unknown ability {raw!r}", field="ability")
    return norm


# ── Runtime dice ──────────────────────────────────────────────────────────


def runtime_d20(count: int, *, rng: Any | None = None) -> list[int]:
    """Generate NPC/hidden d20 results on the runtime path (never for PCs)."""
    if count not in (1, 2):
        raise ResolutionError("invalid_dice_count", f"can generate 1 or 2 d20 results, got {count}", field="dice")
    roller = rng if rng is not None else secrets.SystemRandom()
    try:
        return [int(roller.randint(1, 20)) for _ in range(count)]
    except Exception as exc:
        raise ResolutionError("dice_generation_failed", f"runtime dice generation failed: {exc}", field="dice") from exc


# ── Result DTOs ───────────────────────────────────────────────────────────


class D20Resolution(StrictModel):
    roll_id: str
    kind: RollKind
    target: str
    ability: str | None = None
    advantage_state: AdvantageState = "normal"
    advantage_sources: list[AdvantageSource] = Field(default_factory=list)
    dice_all: list[int] = Field(default_factory=list)
    die_kept: int = 0
    dice_dropped: list[int] = Field(default_factory=list)
    die_source: DieSource = "player_supplied"
    die_visibility: DieVisibility = "public"
    modifier: int = 0
    modifier_components: dict[str, int] = Field(default_factory=dict)
    calculation_path: str = ""
    total: int = 0
    dc: int | None = None
    dc_visibility: DCVisibility = "hidden"
    success: bool | None = None
    status: ResolutionStatus = "resolved"
    is_natural_20: bool = False
    is_natural_1: bool = False
    provenance: dict[str, Any] = Field(default_factory=dict)

    def public_projection(self) -> dict[str, Any]:
        """Observable outcome data: hidden DC/dice stay DM-only unless public."""
        value: dict[str, Any] = {
            "roll_id": self.roll_id,
            "kind": self.kind,
            "target": self.target,
            "advantage_state": self.advantage_state,
            "success": self.success,
            "status": self.status,
            "is_natural_20": self.is_natural_20,
            "is_natural_1": self.is_natural_1,
        }
        if self.die_visibility == "public":
            value.update(die_kept=self.die_kept, dice_all=list(self.dice_all), total=self.total, modifier=self.modifier)
        if self.dc_visibility == "public":
            value["dc"] = self.dc
        return value

    def to_event_payload(self, *, include_private: bool) -> dict[str, Any]:
        if include_private:
            return self.model_dump(mode="json")
        return self.public_projection()


# ── Core resolution ───────────────────────────────────────────────────────


def _validate_dc(dc: int | None) -> int | None:
    if dc is None:
        return None
    if type(dc) is not int or not 1 <= dc <= 1000:
        raise ResolutionError("invalid_dc", f"dc must be an integer 1-1000, got {dc!r}", field="dc")
    return dc


def resolve_d20_roll(
    *,
    kind: RollKind,
    roller: Literal["pc", "npc"] = "pc",
    skill: str | None = None,
    ability: str | None = None,
    sheet: Any | None = None,
    modifier: int | None = None,
    dice: list[int] | None = None,
    advantage_state: AdvantageState | None = None,
    advantage_sources: list[AdvantageSource] | None = None,
    dc: int | None = None,
    dc_visibility: DCVisibility = "hidden",
    die_visibility: DieVisibility | None = None,
    situational_bonus: int = 0,
    extra_modifiers: list[ModifierContribution] | None = None,
    hooks: list[ModifierHook | Callable[[HookContext], int]] | None = None,
    roll_id: str | None = None,
    rng: Any | None = None,
) -> D20Resolution:
    """Deterministically resolve one d20 check or save. Pure — safe to retry.

    - ``roller="pc"``: ``dice`` must be player-supplied (never generated).
    - ``roller="npc"``: ``dice`` may be DM-supplied or runtime-generated.
    - ``advantage_sources`` (2024 cancellation) takes precedence when given;
      otherwise the explicit ``advantage_state`` is used as-is.
    """
    t0 = time.monotonic()
    rid = roll_id or uuid.uuid4().hex[:12]
    invalid_input: str | None = None
    calculation_path = "unresolved"
    logged_state: str = advantage_state or "normal"
    try:
        if kind not in ("check", "save"):
            raise ResolutionError("invalid_roll_kind", f"kind must be check/save, got {kind!r}", field="kind")
        if roller not in ("pc", "npc"):
            raise ResolutionError("invalid_roller", f"roller must be pc/npc, got {roller!r}", field="roller")

        sources = list(advantage_sources or [])
        for source in sources:
            if source not in ("advantage", "disadvantage"):
                raise ResolutionError("invalid_advantage_source", f"unknown advantage source {source!r}", field="advantage_sources")
        state = combine_advantage(sources) if sources else _validate_advantage_state(advantage_state or "normal")
        logged_state = state
        target_dc = _validate_dc(dc)

        resolved = resolve_modifier(
            sheet,
            kind=kind,
            skill=skill,
            ability=ability,
            modifier=modifier,
            situational_bonus=situational_bonus,
            extra_modifiers=extra_modifiers,
            hooks=hooks,
        )
        calculation_path = resolved.calculation_path

        expected_count = 2 if state in ("advantage", "disadvantage") else 1
        if dice is None:
            if roller == "pc":
                raise ResolutionError(
                    "missing_player_die",
                    "PC die results must be supplied by the player; this service never generates them",
                    field="dice",
                )
            dice = runtime_d20(expected_count, rng=rng)
            die_source: DieSource = "runtime_generated"
        else:
            die_source = "player_supplied" if roller == "pc" else "dm_supplied"

        kept, dropped = select_d20(list(dice), state)
        total = kept + resolved.modifier
        is_nat20 = kept == 20
        is_nat1 = kept == 1

        success: bool | None = None
        if target_dc is not None:
            # 2024 D20 Test: natural 20 always succeeds, natural 1 always fails.
            if is_nat20:
                success = True
            elif is_nat1:
                success = False
            else:
                success = total >= target_dc

        visibility: DieVisibility = die_visibility or ("public" if roller == "pc" else "hidden")
        if visibility not in ("public", "hidden"):
            raise ResolutionError("invalid_die_visibility", f"die_visibility must be public/hidden, got {visibility!r}", field="die_visibility")
        if dc_visibility not in ("public", "hidden"):
            raise ResolutionError("invalid_dc_visibility", f"dc_visibility must be public/hidden, got {dc_visibility!r}", field="dc_visibility")

        result = D20Resolution(
            roll_id=rid,
            kind=kind,
            target=str(resolved.provenance.get("target") or skill or ability or ""),
            ability=str(resolved.provenance.get("ability") or ability or "") or None,
            advantage_state=state,
            advantage_sources=sources,
            dice_all=list(dice),
            die_kept=kept,
            dice_dropped=dropped,
            die_source=die_source,
            die_visibility=visibility,
            modifier=resolved.modifier,
            modifier_components=resolved.components,
            calculation_path=resolved.calculation_path,
            total=total,
            dc=target_dc,
            dc_visibility=dc_visibility,
            success=success,
            status="resolved",
            is_natural_20=is_nat20,
            is_natural_1=is_nat1,
            provenance={
                **resolved.provenance,
                "resolution_version": RESOLUTION_VERSION,
                "die_source": die_source,
                "roller": roller,
                "natural_roll_rule": "2024_d20_test" if target_dc is not None else None,
            },
        )
    except ResolutionError as exc:
        invalid_input = exc.code
        raise
    finally:
        latency_ms = round((time.monotonic() - t0) * 1000, 2)
        try:
            structured_log(
                logger,
                logging.INFO,
                "d20_resolution",
                roll_id=rid,
                roll_type=kind,
                calculation_path=calculation_path,
                advantage_state=logged_state,
                invalid_input=invalid_input,
                latency_ms=latency_ms,
                has_hidden_dc=bool(dc is not None and dc_visibility == "hidden"),
                dc_visibility=dc_visibility,
                die_source=("player_supplied" if roller == "pc" and dice is not None else ("runtime" if roller == "npc" else "missing")),
                roller=roller,
                resolution_version=RESOLUTION_VERSION,
            )
        except Exception:
            pass
    return result


def resolve_ability_check(**kwargs: Any) -> D20Resolution:
    """Convenience wrapper for ability/skill checks."""
    return resolve_d20_roll(kind="check", **kwargs)


def resolve_saving_throw(**kwargs: Any) -> D20Resolution:
    """Convenience wrapper for saving throws."""
    return resolve_d20_roll(kind="save", **kwargs)


# ── Duplicate-application guard ───────────────────────────────────────────


class ResolutionLedger:
    """Guard so one logical roll cannot apply a consequence twice.

    Resolution itself is pure (safe to retry); register the ``roll_id`` here
    when its consequence is applied. Returns ``"applied"`` on first use and
    ``"duplicate"`` thereafter.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def apply(self, roll_id: str) -> Literal["applied", "duplicate"]:
        if roll_id in self._applied:
            return "duplicate"
        self._applied.add(roll_id)
        return "applied"

    def is_duplicate(self, roll_id: str) -> bool:
        return roll_id in self._applied

    def __len__(self) -> int:
        return len(self._applied)
