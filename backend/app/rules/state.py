"""Authoritative persistent mechanical state — issue #227.

Deterministic condition / resource / spell-slot / concentration / death-save
/ exhaustion transitions over the canonical character-sheet store (#224).
The AI DM decides *whether* a transition happens; this module performs it
without asking any model to do bookkeeping.

Storage authority (pre-alpha, single canonical impl — no second store):
- PCs: ``Dnd5eCharacterSheet`` columns — ``conditions`` / ``resources`` /
  ``spell_slots`` JSONB, ``extras["concentration"]`` JSONB, death-save and
  ``exhaustion_level`` columns. Mechanical read queries (#224) project these
  directly, so staged state always affects later adjudication.
- NPCs/monsters: ``WorldEntity.details`` — ``conditions`` / ``resources`` /
  ``spell_slots`` / ``concentration`` / ``death_saves`` / ``exhaustion_level``
  keys. Hidden (``dm_only``) NPC state stays DM-only in projections while
  still driving mechanics.

Invariants (mirroring #225/#226):
- Every mutation is pure list/dict/column arithmetic below; persistence
  happens in the staged-effect handlers (``app.dm.effects``) inside the
  turn-commit transaction, so multi-effect mutation is all-or-nothing.
- Every logical mutation carries a caller-supplied stable ``mutation_id``
  (never minted here) — the in-commit and cross-retry dedup identity.
  Duplicate *application* is guarded by :class:`StateLedger`
  (process-local) or, durably across restarts/workers, by
  :func:`apply_state_consequence` via the existing ``app.idempotency``
  durable command record. A retry that resolves to materially different
  payload raises ``IdempotencyConflictError`` rather than double-applying.
- Invalid transitions raise :class:`StateError` before any commit.
- Decision models only ever choose among code-supplied candidates; this
  module performs no model calls.
- Hidden NPC specifics never appear in public projections.

Out of scope: exhaustive class-feature automation, every rare condition
interaction, encounter turn/reaction orchestration.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.observability.tracing import structured_log
from app.rules.mechanics import MECHANICS_VERSION, RULES_REVISION

logger = logging.getLogger(__name__)

STATE_VERSION = "rules_state_v1"

# ── Errors ────────────────────────────────────────────────────────────────


class StateError(ValueError):
    """Explicit invalid-transition state — caller must surface, not guess."""

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


# ── Condition vocabulary (2024 launch play) ───────────────────────────────

COMMON_CONDITIONS: tuple[str, ...] = (
    "blinded",
    "charmed",
    "deafened",
    "frightened",
    "grappled",
    "incapacitated",
    "invisible",
    "paralyzed",
    "petrified",
    "poisoned",
    "prone",
    "restrained",
    "stunned",
    "unconscious",
    "exhaustion",
)

# Conditions that deterministically break concentration when gained — the
# 2024 incapacitated/dead-adjacent set used by the promotion hook. Death
# itself is not a condition; it breaks concentration via the death-save
# handler below.
CONCENTRATION_BREAKING_CONDITIONS = frozenset({
    "incapacitated",
    "paralyzed",
    "petrified",
    "stunned",
    "unconscious",
})

# Conservative 2024 attack-roll interactions for launch play. These are
# *candidate sources* for the #225 advantage-cancellation input — the DM /
# orchestration layer decides applicability (e.g. range for prone); this
# module never invents rolls, it only supplies code-owned candidates.
_ATTACKER_DISADVANTAGE_CONDITIONS = frozenset({
    "blinded",
    "frightened",
    "poisoned",
    "prone",
    "restrained",
})
_DEFENDER_GRANTS_ADVANTAGE = frozenset({
    "paralyzed",
    "petrified",
    "stunned",
    "unconscious",
    "prone",
    "blinded",
    "restrained",
})
_DEFENDER_GRANTS_DISADVANTAGE = frozenset({"invisible"})
# Cannot act at all — orchestration must not request d20 rolls for these.
_CANNOT_ACT_CONDITIONS = frozenset({
    "incapacitated",
    "paralyzed",
    "petrified",
    "stunned",
    "unconscious",
})

ConditionOp = Literal["add", "remove", "update", "tick"]
ResourceOp = Literal["spend", "restore", "set"]
ConcentrationOp = Literal["start", "replace", "break"]
DeathSaveOp = Literal["record", "reset"]
DeathSaveResult = Literal["success", "failure", "critical_success", "critical_failure"]
DeathSaveOutcome = Literal["ongoing", "stabilized", "dead", "revived"]
StateVisibility = Literal["dm_private", "party_known", "public"]
TargetKind = Literal["pc", "npc"]

# Domain-event types (#188 style) for callers committing through
# commit_campaign_mutation. Payloads come from the builders below.
CONDITION_CHANGED_EVENT = "condition.changed"
RESOURCE_CHANGED_EVENT = "resource.changed"
CONCENTRATION_CHANGED_EVENT = "concentration.changed"
DEATH_SAVE_CHANGED_EVENT = "death_save.changed"

# Staged-effect types (code-built only, like #226's apply_attack_damage —
# excluded from the provider schema, rejected by RulesValidator).
APPLY_CONDITION_EFFECT = "apply_condition"
APPLY_RESOURCE_EFFECT = "apply_resource"
APPLY_CONCENTRATION_EFFECT = "apply_concentration"
APPLY_DEATH_SAVE_EFFECT = "apply_death_save"
STATE_EFFECT_TYPES = (
    APPLY_CONDITION_EFFECT,
    APPLY_RESOURCE_EFFECT,
    APPLY_CONCENTRATION_EFFECT,
    APPLY_DEATH_SAVE_EFFECT,
)

_EFFECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_VISIBILITY_VALUES = ("dm_private", "party_known", "public", "dm_only", "campaign", "private")

# Exhaustion bounds (storage-level; per-level semantics stay in rules text).
EXHAUSTION_MIN = 0
EXHAUSTION_MAX = 10


def _require_mutation_id(mutation_id: str | None) -> str:
    """Validate the caller-supplied stable logical mutation ID (never minted)."""
    if not isinstance(mutation_id, str) or not mutation_id.strip() or len(mutation_id) > 128:
        raise StateError(
            "missing_mutation_id",
            "mutation_id is required: supply one stable ID per logical mutation and reuse it on retry",
            field="mutation_id",
        )
    return mutation_id


def _require_effect_id(effect_id: str | None) -> str:
    if (
        not isinstance(effect_id, str)
        or not effect_id.strip()
        or len(effect_id) > 48
        or not _EFFECT_ID_RE.fullmatch(effect_id)
    ):
        raise StateError(
            "invalid_effect_id",
            "effect_id must match [A-Za-z0-9_-]+ (1-48 chars) so the effect survives canonical DM-contract validation",
            field="effect_id",
        )
    return effect_id


def _check_visibility(visibility: str) -> str:
    if visibility not in _VISIBILITY_VALUES:
        raise StateError("invalid_visibility", f"unknown visibility {visibility!r}", field="visibility")
    return visibility


def _observe_invalid(code: str, **fields: Any) -> None:
    try:
        structured_log(logger, logging.WARNING, "rules_state_invalid_transition", invalid_transition=code, **fields)
    except Exception:
        pass


# ── Condition records ─────────────────────────────────────────────────────


class SaveEnds(StrictModel):
    ability: str
    dc: int


class ConditionRecord(StrictModel):
    """Normalized condition state: identity + source + expiry + provenance."""

    name: str
    source: str
    duration_rounds: int | None = None
    save_ends: SaveEnds | None = None
    is_permanent: bool = False
    visibility: str = "public"
    description: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)

    def duration_label(self) -> str | None:
        if self.is_permanent:
            return "permanent"
        if self.duration_rounds is not None and self.save_ends is not None:
            return f"{self.duration_rounds} rounds or save ends ({self.save_ends.ability} {self.save_ends.dc})"
        if self.duration_rounds is not None:
            return f"{self.duration_rounds} rounds"
        if self.save_ends is not None:
            return f"save ends ({self.save_ends.ability} {self.save_ends.dc})"
        return None

    def to_sheet_dict(self) -> dict[str, Any]:
        """Sheet-compatible dict: #224 mechanics keys first, normalized extras kept."""
        return {
            "condition_name": self.name,
            "description": self.description,
            "source": self.source,
            "is_permanent": self.is_permanent,
            "duration_remaining": self.duration_label(),
            "duration_rounds": self.duration_rounds,
            "save_ends": self.save_ends.model_dump(mode="json") if self.save_ends else None,
            "visibility": self.visibility,
            "provenance": dict(self.provenance),
        }


def normalize_condition_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise StateError("invalid_condition", f"condition name must be a non-empty string, got {raw!r}", field="condition")
    norm = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if norm not in COMMON_CONDITIONS:
        _observe_invalid("invalid_condition", condition=raw)
        raise StateError(
            "invalid_condition",
            f"unknown condition {raw!r}: supported launch-play conditions are {', '.join(COMMON_CONDITIONS)}",
            field="condition",
            details={"supported": list(COMMON_CONDITIONS)},
        )
    return norm


def _condition_key(item: dict[str, Any]) -> str:
    for key in ("condition_name", "name", "condition", "title"):
        if isinstance(item.get(key), str) and item[key].strip():
            return str(item[key]).strip().lower().replace(" ", "_").replace("-", "_")
    return ""


def parse_condition_record(item: dict[str, Any]) -> ConditionRecord | None:
    """Best-effort parse of a stored condition dict; None when unparseable."""
    if not isinstance(item, dict):
        return None
    name = _condition_key(item)
    if name not in COMMON_CONDITIONS:
        return None
    save_ends = None
    raw_save = item.get("save_ends")
    if isinstance(raw_save, dict) and raw_save.get("ability") is not None and raw_save.get("dc") is not None:
        try:
            save_ends = SaveEnds(ability=str(raw_save["ability"]), dc=int(raw_save["dc"]))
        except Exception:
            save_ends = None
    rounds = item.get("duration_rounds")
    try:
        rounds = int(rounds) if rounds is not None else None
    except Exception:
        rounds = None
    provenance = item.get("provenance")
    return ConditionRecord(
        name=name,
        source=str(item.get("source") or "unknown"),
        duration_rounds=rounds,
        save_ends=save_ends,
        is_permanent=bool(item.get("is_permanent", False)),
        visibility=str(item.get("visibility") or "public"),
        description=item.get("description"),
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
    )


def _validate_save_ends(raw: Any) -> SaveEnds | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise StateError("invalid_save_ends", "save_ends must be an object with ability + dc", field="save_ends")
    ability = str(raw.get("ability") or "").strip().lower()
    if ability not in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"):
        raise StateError("invalid_save_ends", f"unknown save ability {raw.get('ability')!r}", field="save_ends")
    try:
        dc = int(raw.get("dc"))
    except (TypeError, ValueError):
        raise StateError("invalid_save_ends", f"save dc must be an integer, got {raw.get('dc')!r}", field="save_ends") from None
    if not 1 <= dc <= 30:
        raise StateError("invalid_save_ends", f"save dc {dc} outside 1-30", field="save_ends")
    return SaveEnds(ability=ability, dc=dc)


def add_condition(
    conditions: list[dict[str, Any]] | None,
    *,
    name: str,
    source: str,
    duration_rounds: int | None = None,
    save_ends: dict[str, Any] | None = None,
    is_permanent: bool = False,
    visibility: str = "public",
    description: str | None = None,
    provenance: dict[str, Any] | None = None,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], ConditionRecord]:
    """Add one condition. Pure — duplicate names and unknown names fail closed."""
    mid = _require_mutation_id(mutation_id)
    norm = normalize_condition_name(name)
    if not isinstance(source, str) or not source.strip():
        raise StateError("missing_source", "condition source is required provenance (spell, feature, or effect)", field="source")
    if duration_rounds is not None and (type(duration_rounds) is not int or duration_rounds < 1):
        raise StateError("invalid_duration", f"duration_rounds must be an integer >= 1, got {duration_rounds!r}", field="duration_rounds")
    if type(is_permanent) is not bool:
        raise StateError("invalid_duration", "is_permanent must be bool", field="is_permanent")
    if duration_rounds is not None and is_permanent:
        raise StateError("invalid_duration", "a condition cannot have both duration_rounds and is_permanent", field="duration_rounds")
    parsed_save = _validate_save_ends(save_ends)
    _check_visibility(visibility)

    current = [dict(item) for item in (conditions or []) if isinstance(item, dict)]
    if any(_condition_key(item) == norm for item in current):
        _observe_invalid("duplicate_condition", condition=norm, mutation_id=mid)
        raise StateError("duplicate_condition", f"condition {norm!r} is already present: update it instead of re-adding", field="condition")
    record = ConditionRecord(
        name=norm,
        source=source.strip(),
        duration_rounds=duration_rounds,
        save_ends=parsed_save,
        is_permanent=is_permanent,
        visibility=visibility,
        description=description,
        provenance=dict(provenance or {}),
    )
    current.append(record.to_sheet_dict())
    try:
        structured_log(
            logger, logging.INFO, "rules_state_condition_add",
            mutation_id=mid, condition=norm, source=record.source,
            duration_rounds=duration_rounds, is_permanent=is_permanent,
            state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return current, record


def remove_condition(
    conditions: list[dict[str, Any]] | None,
    *,
    name: str,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], ConditionRecord]:
    """Remove one condition. Pure — removing an absent condition fails closed."""
    mid = _require_mutation_id(mutation_id)
    norm = normalize_condition_name(name)
    current = [dict(item) for item in (conditions or []) if isinstance(item, dict)]
    removed: ConditionRecord | None = None
    remaining: list[dict[str, Any]] = []
    for item in current:
        if _condition_key(item) == norm and removed is None:
            removed = parse_condition_record(item) or ConditionRecord(name=norm, source=str(item.get("source") or "unknown"))
        else:
            remaining.append(item)
    if removed is None:
        _observe_invalid("condition_not_present", condition=norm, mutation_id=mid)
        raise StateError("condition_not_present", f"condition {norm!r} is not present and cannot be removed", field="condition")
    try:
        structured_log(logger, logging.INFO, "rules_state_condition_remove", mutation_id=mid, condition=norm, state_version=STATE_VERSION)
    except Exception:
        pass
    return remaining, removed


def update_condition(
    conditions: list[dict[str, Any]] | None,
    *,
    name: str,
    mutation_id: str | None = None,
    source: str | None = None,
    duration_rounds: int | None = None,
    clear_duration: bool = False,
    save_ends: dict[str, Any] | None = None,
    clear_save_ends: bool = False,
    is_permanent: bool | None = None,
    visibility: str | None = None,
    description: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], ConditionRecord]:
    """Update source/duration/visibility of a present condition. Pure."""
    mid = _require_mutation_id(mutation_id)
    norm = normalize_condition_name(name)
    current = [dict(item) for item in (conditions or []) if isinstance(item, dict)]
    idx = next((i for i, item in enumerate(current) if _condition_key(item) == norm), None)
    if idx is None:
        _observe_invalid("condition_not_present", condition=norm, mutation_id=mid)
        raise StateError("condition_not_present", f"condition {norm!r} is not present and cannot be updated", field="condition")
    record = parse_condition_record(current[idx]) or ConditionRecord(name=norm, source="unknown")
    data = record.model_dump()
    if source is not None:
        if not isinstance(source, str) or not source.strip():
            raise StateError("missing_source", "condition source must be a non-empty string", field="source")
        data["source"] = source.strip()
    if clear_duration:
        data["duration_rounds"] = None
    elif duration_rounds is not None:
        if type(duration_rounds) is not int or duration_rounds < 1:
            raise StateError("invalid_duration", f"duration_rounds must be an integer >= 1, got {duration_rounds!r}", field="duration_rounds")
        data["duration_rounds"] = duration_rounds
    if clear_save_ends:
        data["save_ends"] = None
    elif save_ends is not None:
        data["save_ends"] = _validate_save_ends(save_ends)
    if is_permanent is not None:
        if type(is_permanent) is not bool:
            raise StateError("invalid_duration", "is_permanent must be bool", field="is_permanent")
        data["is_permanent"] = is_permanent
    if data["duration_rounds"] is not None and data["is_permanent"]:
        raise StateError("invalid_duration", "a condition cannot have both duration_rounds and is_permanent", field="duration_rounds")
    if visibility is not None:
        data["visibility"] = _check_visibility(visibility)
    if description is not None:
        data["description"] = description
    if provenance is not None:
        if not isinstance(provenance, dict):
            raise StateError("invalid_provenance", "provenance must be an object", field="provenance")
        data["provenance"] = dict(provenance)
    updated = ConditionRecord.model_validate(data)
    current[idx] = updated.to_sheet_dict()
    try:
        structured_log(logger, logging.INFO, "rules_state_condition_update", mutation_id=mid, condition=norm, state_version=STATE_VERSION)
    except Exception:
        pass
    return current, updated


def tick_conditions(
    conditions: list[dict[str, Any]] | None,
    *,
    rounds: int = 1,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], list[ConditionRecord]]:
    """Advance duration clocks by ``rounds``; timed-out entries expire. Pure.

    Permanent and save-ends-only entries never expire on a tick — a save
    ends them via ``remove_condition`` when the DM adjudicates the save.
    """
    mid = _require_mutation_id(mutation_id)
    if type(rounds) is not int or rounds < 1:
        raise StateError("invalid_duration", f"rounds must be an integer >= 1, got {rounds!r}", field="rounds")
    remaining: list[dict[str, Any]] = []
    expired: list[ConditionRecord] = []
    for item in (conditions or []):
        if not isinstance(item, dict):
            continue
        record = parse_condition_record(item)
        if record is None or record.is_permanent or record.duration_rounds is None:
            remaining.append(dict(item))
            continue
        left = record.duration_rounds - rounds
        if left <= 0:
            expired.append(record)
        else:
            record.duration_rounds = left
            remaining.append(record.to_sheet_dict())
    try:
        structured_log(
            logger, logging.INFO, "rules_state_condition_tick",
            mutation_id=mid, rounds=rounds,
            expired=[r.name for r in expired], remaining=len(remaining),
            state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return remaining, expired


# ── Mechanical condition queries ──────────────────────────────────────────


def has_condition(conditions: list[dict[str, Any]] | None, name: str) -> bool:
    try:
        norm = normalize_condition_name(name)
    except StateError:
        return False
    return any(_condition_key(item) == norm for item in (conditions or []) if isinstance(item, dict))


def active_condition_names(conditions: list[dict[str, Any]] | None) -> list[str]:
    out: list[str] = []
    for item in (conditions or []):
        if not isinstance(item, dict):
            continue
        key = _condition_key(item)
        if key in COMMON_CONDITIONS and key not in out:
            out.append(key)
    return out


def attacker_condition_sources(conditions: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Code-owned candidate sources for #225 advantage cancellation + act check.

    Returns ``{"disadvantage": [...], "cannot_act": [...]}`` naming the
    conditions behind each candidate. The DM decides applicability; this only
    supplies the candidate space.
    """
    names = set(active_condition_names(conditions))
    return {
        "disadvantage": sorted(names & _ATTACKER_DISADVANTAGE_CONDITIONS),
        "cannot_act": sorted(names & _CANNOT_ACT_CONDITIONS),
    }


def defender_granted_sources(conditions: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Candidate advantage/disadvantage the defender's conditions grant attackers."""
    names = set(active_condition_names(conditions))
    return {
        "advantage": sorted(names & _DEFENDER_GRANTS_ADVANTAGE),
        "disadvantage": sorted(names & _DEFENDER_GRANTS_DISADVANTAGE),
    }


# ── Tracked resources ─────────────────────────────────────────────────────


def _resource_key(item: dict[str, Any]) -> str:
    for key in ("name", "resource_name", "title"):
        if isinstance(item.get(key), str) and item[key].strip():
            return str(item[key]).strip().lower()
    return ""


def _resource_max(item: dict[str, Any]) -> int:
    for key in ("maximum", "max"):
        if item.get(key) is not None:
            return int(item[key])
    return 0


def spend_resource(
    resources: list[dict[str, Any]] | None,
    *,
    name: str,
    amount: int = 1,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Spend ``amount`` of a tracked resource. Pure — overdraft fails closed."""
    mid = _require_mutation_id(mutation_id)
    if not isinstance(name, str) or not name.strip():
        raise StateError("resource_not_found", "resource name is required", field="resource")
    if type(amount) is not int or amount < 1:
        raise StateError("invalid_amount", f"spend amount must be an integer >= 1, got {amount!r}", field="amount")
    current = [dict(item) for item in (resources or []) if isinstance(item, dict)]
    idx = next((i for i, item in enumerate(current) if _resource_key(item) == name.strip().lower()), None)
    if idx is None:
        _observe_invalid("resource_not_found", resource=name, mutation_id=mid)
        raise StateError("resource_not_found", f"resource {name!r} is not tracked", field="resource")
    try:
        before = int(current[idx].get("current", 0) or 0)
        maximum = _resource_max(current[idx])
    except (TypeError, ValueError):
        raise StateError("malformed_resource", f"resource {name!r} counts are malformed", field="resource") from None
    if before - amount < 0:
        _observe_invalid("insufficient_resource", resource=name, current=before, amount=amount, mutation_id=mid)
        raise StateError(
            "insufficient_resource",
            f"resource {name!r} has {before}, cannot spend {amount}",
            field="resource",
            details={"current": before, "amount": amount},
        )
    current[idx]["current"] = before - amount
    delta = {"before": before, "spent": amount, "after": before - amount, "maximum": maximum}
    try:
        structured_log(logger, logging.INFO, "rules_state_resource_spend", mutation_id=mid, resource=name, **delta, state_version=STATE_VERSION)
    except Exception:
        pass
    return current, delta


def restore_resource(
    resources: list[dict[str, Any]] | None,
    *,
    name: str,
    amount: int = 1,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Restore ``amount`` of a tracked resource, capped at maximum. Pure."""
    mid = _require_mutation_id(mutation_id)
    if not isinstance(name, str) or not name.strip():
        raise StateError("resource_not_found", "resource name is required", field="resource")
    if type(amount) is not int or amount < 1:
        raise StateError("invalid_amount", f"restore amount must be an integer >= 1, got {amount!r}", field="amount")
    current = [dict(item) for item in (resources or []) if isinstance(item, dict)]
    idx = next((i for i, item in enumerate(current) if _resource_key(item) == name.strip().lower()), None)
    if idx is None:
        _observe_invalid("resource_not_found", resource=name, mutation_id=mid)
        raise StateError("resource_not_found", f"resource {name!r} is not tracked", field="resource")
    try:
        before = int(current[idx].get("current", 0) or 0)
        maximum = _resource_max(current[idx])
    except (TypeError, ValueError):
        raise StateError("malformed_resource", f"resource {name!r} counts are malformed", field="resource") from None
    after = min(maximum, before + amount)
    current[idx]["current"] = after
    delta = {"before": before, "restored": after - before, "after": after, "maximum": maximum}
    try:
        structured_log(logger, logging.INFO, "rules_state_resource_restore", mutation_id=mid, resource=name, **delta, state_version=STATE_VERSION)
    except Exception:
        pass
    return current, delta


def set_resource(
    resources: list[dict[str, Any]] | None,
    *,
    name: str,
    current: int | None = None,
    maximum: int | None = None,
    mutation_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Set resource current/maximum within legal bounds. Pure.

    Lowering ``maximum`` below the stored current deterministically clamps
    current to the new maximum (documented convergence, not caller order).
    An explicit ``current`` above the effective maximum fails closed.
    """
    mid = _require_mutation_id(mutation_id)
    if not isinstance(name, str) or not name.strip():
        raise StateError("resource_not_found", "resource name is required", field="resource")
    tracked = [dict(item) for item in (resources or []) if isinstance(item, dict)]
    idx = next((i for i, item in enumerate(tracked) if _resource_key(item) == name.strip().lower()), None)
    if idx is None:
        _observe_invalid("resource_not_found", resource=name, mutation_id=mid)
        raise StateError("resource_not_found", f"resource {name!r} is not tracked", field="resource")
    try:
        old_current = int(tracked[idx].get("current", 0) or 0)
        old_max = _resource_max(tracked[idx])
    except (TypeError, ValueError):
        raise StateError("malformed_resource", f"resource {name!r} counts are malformed", field="resource") from None
    new_max = old_max if maximum is None else maximum
    if type(new_max) is not int or new_max < 0:
        raise StateError("invalid_resource_bounds", f"maximum must be an integer >= 0, got {maximum!r}", field="maximum")
    if current is None:
        new_current = min(old_current, new_max)
    else:
        if type(current) is not int or current < 0:
            raise StateError("invalid_resource_bounds", f"current must be an integer >= 0, got {current!r}", field="current")
        if current > new_max:
            _observe_invalid("invalid_resource_bounds", resource=name, current=current, maximum=new_max, mutation_id=mid)
            raise StateError(
                "invalid_resource_bounds",
                f"resource {name!r} current {current} exceeds maximum {new_max}",
                field="current",
                details={"current": current, "maximum": new_max},
            )
        new_current = current
    tracked[idx]["current"] = new_current
    if "maximum" in tracked[idx]:
        tracked[idx]["maximum"] = new_max
    elif "max" in tracked[idx]:
        tracked[idx]["max"] = new_max
    else:
        tracked[idx]["maximum"] = new_max
    delta = {"before": old_current, "after": new_current, "maximum": new_max, "previous_maximum": old_max}
    try:
        structured_log(logger, logging.INFO, "rules_state_resource_set", mutation_id=mid, resource=name, **delta, state_version=STATE_VERSION)
    except Exception:
        pass
    return tracked, delta


# ── Spell slots ───────────────────────────────────────────────────────────


def _slot_entry(slots: dict[str, Any], level: int) -> tuple[str, int, int]:
    key = str(level)
    if key not in slots:
        raise StateError("unknown_slot_level", f"no spell slots tracked for level {level}", field="slot_level")
    raw = slots[key]
    if isinstance(raw, int):
        return key, raw, 0
    if not isinstance(raw, dict):
        raise StateError("malformed_spell_slots", f"slot level {level} entry is malformed", field="slot_level")
    try:
        maximum = int(raw.get("max", raw.get("maximum", 0)) or 0)
        used = int(raw.get("used", 0) or 0)
    except (TypeError, ValueError):
        raise StateError("malformed_spell_slots", f"slot level {level} counts are malformed", field="slot_level") from None
    if maximum < 0 or used < 0 or used > maximum:
        raise StateError("malformed_spell_slots", f"slot level {level} has used {used} > max {maximum}", field="slot_level")
    return key, maximum, used


def spend_spell_slot(
    slots: dict[str, Any] | None,
    *,
    level: int,
    mutation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Spend one spell slot of ``level``. Pure — exhausted levels fail closed."""
    mid = _require_mutation_id(mutation_id)
    if type(level) is not int or not 1 <= level <= 9:
        raise StateError("unknown_slot_level", f"slot level must be an integer 1-9, got {level!r}", field="slot_level")
    current = dict(slots or {})
    try:
        key, maximum, used = _slot_entry(current, level)
    except StateError as exc:
        _observe_invalid(exc.code, slot_level=level, mutation_id=mid)
        raise
    if used >= maximum:
        _observe_invalid("no_slots_remaining", slot_level=level, maximum=maximum, mutation_id=mid)
        raise StateError(
            "no_slots_remaining",
            f"no level-{level} spell slots remaining (used {used}/{maximum})",
            field="slot_level",
            details={"level": level, "used": used, "maximum": maximum},
        )
    current[key] = {"max": maximum, "used": used + 1, "remaining": maximum - used - 1}
    delta = {"level": level, "before_used": used, "after_used": used + 1, "maximum": maximum, "remaining": maximum - used - 1}
    try:
        structured_log(logger, logging.INFO, "rules_state_slot_spend", mutation_id=mid, **delta, state_version=STATE_VERSION)
    except Exception:
        pass
    return current, delta


def restore_spell_slot(
    slots: dict[str, Any] | None,
    *,
    level: int,
    amount: int = 1,
    mutation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Restore ``amount`` spell slots of ``level`` (used floors at 0). Pure."""
    mid = _require_mutation_id(mutation_id)
    if type(level) is not int or not 1 <= level <= 9:
        raise StateError("unknown_slot_level", f"slot level must be an integer 1-9, got {level!r}", field="slot_level")
    if type(amount) is not int or amount < 1:
        raise StateError("invalid_amount", f"restore amount must be an integer >= 1, got {amount!r}", field="amount")
    current = dict(slots or {})
    try:
        key, maximum, used = _slot_entry(current, level)
    except StateError as exc:
        _observe_invalid(exc.code, slot_level=level, mutation_id=mid)
        raise
    restored = min(used, amount)
    current[key] = {"max": maximum, "used": used - restored, "remaining": maximum - used + restored}
    delta = {"level": level, "before_used": used, "after_used": used - restored, "restored": restored, "maximum": maximum}
    try:
        structured_log(logger, logging.INFO, "rules_state_slot_restore", mutation_id=mid, **delta, state_version=STATE_VERSION)
    except Exception:
        pass
    return current, delta


# ── Concentration ─────────────────────────────────────────────────────────


class ConcentrationState(StrictModel):
    active: bool = False
    effect_name: str | None = None
    effect_id: str | None = None
    source: str | None = None
    visibility: str = "public"
    provenance: dict[str, Any] = Field(default_factory=dict)


_CONCENTRATION_BREAK_REASONS = ("damage", "new_concentration", "incapacitated", "dead", "dismissed", "rest", "condition")


def parse_concentration(raw: Any) -> ConcentrationState:
    if not isinstance(raw, dict) or not raw.get("active"):
        return ConcentrationState(active=False)
    try:
        return ConcentrationState.model_validate({**raw, "active": True})
    except Exception:
        return ConcentrationState(active=False)


def start_concentration(
    current: dict[str, Any] | None,
    *,
    effect_name: str,
    effect_id: str,
    source: str,
    visibility: str = "public",
    provenance: dict[str, Any] | None = None,
    mutation_id: str | None = None,
) -> tuple[dict[str, Any], ConcentrationState]:
    """Begin concentrating. Pure — starting while active fails closed (replace instead)."""
    mid = _require_mutation_id(mutation_id)
    state = parse_concentration(current)
    if state.active:
        _observe_invalid("concentration_conflict", current=state.effect_name, mutation_id=mid)
        raise StateError(
            "concentration_conflict",
            f"already concentrating on {state.effect_name!r}: replace or break it first",
            field="concentration",
            details={"current_effect": state.effect_name, "current_effect_id": state.effect_id},
        )
    for label, value in (("effect_name", effect_name), ("effect_id", effect_id), ("source", source)):
        if not isinstance(value, str) or not value.strip():
            raise StateError("missing_concentration_effect", f"concentration {label} is required", field="concentration")
    _check_visibility(visibility)
    if provenance is not None and not isinstance(provenance, dict):
        raise StateError("invalid_provenance", "provenance must be an object", field="provenance")
    record = ConcentrationState(
        active=True,
        effect_name=effect_name.strip(),
        effect_id=effect_id.strip(),
        source=source.strip(),
        visibility=visibility,
        provenance=dict(provenance or {}),
    )
    try:
        structured_log(logger, logging.INFO, "rules_state_concentration_start", mutation_id=mid, effect=record.effect_name, state_version=STATE_VERSION)
    except Exception:
        pass
    return record.model_dump(mode="json"), record


def replace_concentration(
    current: dict[str, Any] | None,
    *,
    effect_name: str,
    effect_id: str,
    source: str,
    visibility: str = "public",
    provenance: dict[str, Any] | None = None,
    mutation_id: str | None = None,
) -> tuple[dict[str, Any], ConcentrationState, ConcentrationState | None]:
    """Replace concentration (casting another concentration spell ends the first).

    Safe on empty state — behaves as a start and reports ``broke=None``.
    Returns ``(new_state, started, broke_or_none)``. Pure.
    """
    mid = _require_mutation_id(mutation_id)
    state = parse_concentration(current)
    broke = state if state.active else None
    # Validate the incoming effect before dropping the old one.
    for label, value in (("effect_name", effect_name), ("effect_id", effect_id), ("source", source)):
        if not isinstance(value, str) or not value.strip():
            raise StateError("missing_concentration_effect", f"concentration {label} is required", field="concentration")
    _check_visibility(visibility)
    if provenance is not None and not isinstance(provenance, dict):
        raise StateError("invalid_provenance", "provenance must be an object", field="provenance")
    started = ConcentrationState(
        active=True,
        effect_name=effect_name.strip(),
        effect_id=effect_id.strip(),
        source=source.strip(),
        visibility=visibility,
        provenance=dict(provenance or {}),
    )
    try:
        structured_log(
            logger, logging.INFO, "rules_state_concentration_replace",
            mutation_id=mid, broke=(broke.effect_name if broke else None),
            started=started.effect_name, state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return started.model_dump(mode="json"), started, broke


def break_concentration(
    current: dict[str, Any] | None,
    *,
    reason: str,
    mutation_id: str | None = None,
) -> tuple[dict[str, Any], ConcentrationState]:
    """Break concentration. Pure — breaking with none active fails closed."""
    mid = _require_mutation_id(mutation_id)
    if reason not in _CONCENTRATION_BREAK_REASONS:
        raise StateError(
            "invalid_concentration_reason",
            f"unknown break reason {reason!r}: expected one of {', '.join(_CONCENTRATION_BREAK_REASONS)}",
            field="reason",
        )
    state = parse_concentration(current)
    if not state.active:
        _observe_invalid("no_concentration", reason=reason, mutation_id=mid)
        raise StateError("no_concentration", "no active concentration to break", field="concentration")
    try:
        structured_log(
            logger, logging.INFO, "rules_state_concentration_break",
            mutation_id=mid, effect=state.effect_name, reason=reason, state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return ConcentrationState(active=False).model_dump(mode="json"), state


# ── Death saves ───────────────────────────────────────────────────────────


class DeathSaveState(StrictModel):
    successes: int = 0
    failures: int = 0
    outcome: DeathSaveOutcome = "ongoing"
    stabilized: bool = False
    dead: bool = False
    revived_hp: int = 0


def _validate_death_counters(successes: Any, failures: Any) -> tuple[int, int]:
    if type(successes) is not int or not 0 <= successes <= 3:
        raise StateError("invalid_death_counters", f"successes must be an integer 0-3, got {successes!r}", field="death_saves")
    if type(failures) is not int or not 0 <= failures <= 3:
        raise StateError("invalid_death_counters", f"failures must be an integer 0-3, got {failures!r}", field="death_saves")
    return successes, failures


def record_death_save(
    successes: int,
    failures: int,
    *,
    result: DeathSaveResult,
    mutation_id: str | None = None,
) -> DeathSaveState:
    """Advance death-save counters per baseline 2024 rules. Pure.

    - 3 successes → ``stabilized`` (unconscious, no further saves).
    - 3 failures → ``dead``.
    - critical_success (natural 20) → ``revived`` with 1 HP + counters reset.
    - critical_failure (natural 1) → two failures.
    Recording against already-resolved counters fails closed.
    """
    mid = _require_mutation_id(mutation_id)
    successes, failures = _validate_death_counters(successes, failures)
    if result not in ("success", "failure", "critical_success", "critical_failure"):
        raise StateError("invalid_death_result", f"unknown death-save result {result!r}", field="result")
    if successes >= 3 or failures >= 3:
        _observe_invalid("death_save_resolved", successes=successes, failures=failures, mutation_id=mid)
        raise StateError(
            "death_save_resolved",
            "death saves are already resolved (3 successes or 3 failures): reset them first",
            field="death_saves",
            details={"successes": successes, "failures": failures},
        )
    new_s, new_f = successes, failures
    revived_hp = 0
    if result == "success":
        new_s += 1
    elif result == "failure":
        new_f += 1
    elif result == "critical_failure":
        new_f += 2
    else:  # critical_success: natural 20 — regain 1 HP, counters reset
        new_s, new_f = 0, 0
        revived_hp = 1
    outcome: DeathSaveOutcome = "ongoing"
    stabilized = new_s >= 3
    dead = new_f >= 3
    if revived_hp:
        outcome = "revived"
    elif dead:
        outcome = "dead"
    elif stabilized:
        outcome = "stabilized"
    state = DeathSaveState(
        successes=new_s, failures=new_f, outcome=outcome,
        stabilized=stabilized, dead=dead, revived_hp=revived_hp,
    )
    try:
        structured_log(
            logger, logging.INFO, "rules_state_death_save",
            mutation_id=mid, result=result, successes=new_s, failures=new_f,
            outcome=outcome, state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return state


def reset_death_saves(
    successes: int,
    failures: int,
    *,
    reason: str,
    mutation_id: str | None = None,
) -> DeathSaveState:
    """Reset counters after healing, stabilization care, or rest. Pure."""
    mid = _require_mutation_id(mutation_id)
    _validate_death_counters(successes, failures)
    if reason not in ("healed", "stabilized", "rest", "revived"):
        raise StateError(
            "invalid_death_reset_reason",
            f"unknown reset reason {reason!r}: expected healed/stabilized/rest/revived",
            field="reason",
        )
    try:
        structured_log(
            logger, logging.INFO, "rules_state_death_save_reset",
            mutation_id=mid, reason=reason, cleared_successes=successes,
            cleared_failures=failures, state_version=STATE_VERSION,
        )
    except Exception:
        pass
    return DeathSaveState(successes=0, failures=0, outcome="ongoing")


# ── Exhaustion ────────────────────────────────────────────────────────────


def set_exhaustion(
    level: int,
    *,
    mutation_id: str | None = None,
) -> int:
    """Validate an exhaustion level transition. Pure (persistence in handlers)."""
    mid = _require_mutation_id(mutation_id)
    if type(level) is not int or not EXHAUSTION_MIN <= level <= EXHAUSTION_MAX:
        _observe_invalid("invalid_exhaustion", level=level, mutation_id=mid)
        raise StateError(
            "invalid_exhaustion",
            f"exhaustion level must be an integer {EXHAUSTION_MIN}-{EXHAUSTION_MAX}, got {level!r}",
            field="exhaustion_level",
        )
    try:
        structured_log(logger, logging.INFO, "rules_state_exhaustion_set", mutation_id=mid, level=level, state_version=STATE_VERSION)
    except Exception:
        pass
    return level


# ── Staged-effect builders (code-built only) ──────────────────────────────


def _base_effect(
    *,
    effect_id: str | None,
    mutation_id: str | None,
    target_kind: str,
    target_id: str,
    visibility: str = "dm_private",
) -> dict[str, Any]:
    _require_effect_id(effect_id)
    mid = _require_mutation_id(mutation_id)
    if target_kind not in ("pc", "npc"):
        raise StateError("invalid_target", f"target_kind must be pc/npc, got {target_kind!r}", field="target_kind")
    try:
        import uuid as _uuid

        _uuid.UUID(str(target_id))
    except ValueError:
        raise StateError("invalid_target", "target_id must be a UUID", field="target_id") from None
    _check_visibility(visibility)
    return {
        "id": effect_id,
        "arguments": {
            "target_kind": target_kind,
            "target_id": str(target_id),
            "mutation_id": mid,
            "visibility": visibility,
        },
    }


def build_condition_effect(
    *,
    effect_id: str,
    mutation_id: str,
    target_kind: TargetKind,
    target_id: str,
    op: ConditionOp,
    condition: str | None = None,
    source: str | None = None,
    duration_rounds: int | None = None,
    clear_duration: bool = False,
    save_ends: dict[str, Any] | None = None,
    clear_save_ends: bool = False,
    is_permanent: bool | None = None,
    visibility: str = "dm_private",
    description: str | None = None,
    provenance: dict[str, Any] | None = None,
    rounds: int = 1,
    exhaustion_level: int | None = None,
) -> dict[str, Any]:
    """Typed staged-effect record for condition add/remove/update/tick (#206 registry).

    Defaults to ``dm_private`` so promotion from a private attempt cannot
    broaden disclosure; callers widen explicitly for shared-table state.
    Exhaustion transitions ride on ``op`` with ``condition="exhaustion"``
    plus ``exhaustion_level`` so the column and the structural entry commit
    together.
    """
    if op not in ("add", "remove", "update", "tick"):
        raise StateError("invalid_condition_op", f"unknown condition op {op!r}", field="op")
    # Pre-commit validation: bad transitions are rejected before staging.
    if op in ("add", "remove", "update"):
        normalize_condition_name(condition)
    if op == "add" and (not isinstance(source, str) or not source.strip()):
        raise StateError("missing_source", "condition add requires a source", field="source")
    if duration_rounds is not None and (type(duration_rounds) is not int or duration_rounds < 1):
        raise StateError("invalid_duration", f"duration_rounds must be an integer >= 1, got {duration_rounds!r}", field="duration_rounds")
    if type(clear_duration) is not bool or type(clear_save_ends) is not bool:
        raise StateError("invalid_duration", "clear_duration/clear_save_ends must be bool", field="duration_rounds")
    if save_ends is not None:
        _validate_save_ends(save_ends)
    if is_permanent is not None and type(is_permanent) is not bool:
        raise StateError("invalid_duration", "is_permanent must be bool", field="is_permanent")
    if duration_rounds is not None and is_permanent:
        raise StateError("invalid_duration", "a condition cannot have both duration_rounds and is_permanent", field="duration_rounds")
    if op == "tick" and (type(rounds) is not int or rounds < 1):
        raise StateError("invalid_duration", f"rounds must be an integer >= 1, got {rounds!r}", field="rounds")
    if isinstance(condition, str) and condition.strip().lower() == "exhaustion" and op in ("add", "update"):
        if exhaustion_level is None or type(exhaustion_level) is not int:
            raise StateError("invalid_exhaustion", "exhaustion add/update requires an integer exhaustion_level", field="exhaustion_level")
        set_exhaustion(exhaustion_level, mutation_id=mutation_id)
    if provenance is not None and not isinstance(provenance, dict):
        raise StateError("invalid_provenance", "provenance must be an object", field="provenance")
    base = _base_effect(effect_id=effect_id, mutation_id=mutation_id, target_kind=target_kind, target_id=target_id, visibility=visibility)
    base["effect_type"] = APPLY_CONDITION_EFFECT
    base["arguments"].update({
        "op": op,
        "condition": condition,
        "source": source,
        "duration_rounds": duration_rounds,
        "clear_duration": clear_duration,
        "save_ends": save_ends,
        "clear_save_ends": clear_save_ends,
        "is_permanent": is_permanent,
        "description": description,
        "provenance": provenance,
        "rounds": rounds,
        "exhaustion_level": exhaustion_level,
    })
    return base


def build_resource_effect(
    *,
    effect_id: str,
    mutation_id: str,
    target_kind: TargetKind,
    target_id: str,
    op: ResourceOp,
    resource: str | None = None,
    amount: int = 1,
    current: int | None = None,
    maximum: int | None = None,
    slot_level: int | None = None,
    visibility: str = "dm_private",
) -> dict[str, Any]:
    """Typed staged-effect record for resource spend/restore/set or slot use.

    ``resource="spell_slots:N"`` (or ``slot_level=N``) addresses spell slots;
    any other name addresses the tracked-resources list.
    """
    if op not in ("spend", "restore", "set"):
        raise StateError("invalid_resource_op", f"unknown resource op {op!r}", field="op")
    slot: int | None = slot_level
    resource_name = resource
    if isinstance(resource, str) and resource.strip().lower().startswith("spell_slots:"):
        try:
            slot = int(resource.strip().split(":", 1)[1])
        except ValueError:
            raise StateError("unknown_slot_level", f"unparseable slot resource {resource!r}", field="resource") from None
        resource_name = None
    if slot is not None:
        if type(slot) is not int or not 1 <= slot <= 9:
            raise StateError("unknown_slot_level", f"slot level must be an integer 1-9, got {slot!r}", field="slot_level")
        if op == "set":
            raise StateError("invalid_resource_op", "spell slots support spend/restore only, not set", field="op")
        if type(amount) is not int or amount < 1:
            raise StateError("invalid_amount", f"amount must be an integer >= 1, got {amount!r}", field="amount")
    else:
        if not isinstance(resource_name, str) or not resource_name.strip():
            raise StateError("resource_not_found", "resource name is required", field="resource")
        if op in ("spend", "restore") and (type(amount) is not int or amount < 1):
            raise StateError("invalid_amount", f"amount must be an integer >= 1, got {amount!r}", field="amount")
        if op == "set":
            if current is not None and (type(current) is not int or current < 0):
                raise StateError("invalid_resource_bounds", f"current must be an integer >= 0, got {current!r}", field="current")
            if maximum is not None and (type(maximum) is not int or maximum < 0):
                raise StateError("invalid_resource_bounds", f"maximum must be an integer >= 0, got {maximum!r}", field="maximum")
            if current is not None and maximum is not None and current > maximum:
                raise StateError("invalid_resource_bounds", f"current {current} exceeds maximum {maximum}", field="current")
    base = _base_effect(effect_id=effect_id, mutation_id=mutation_id, target_kind=target_kind, target_id=target_id, visibility=visibility)
    base["effect_type"] = APPLY_RESOURCE_EFFECT
    base["arguments"].update({
        "op": op,
        "resource": resource_name,
        "amount": amount,
        "current": current,
        "maximum": maximum,
        "slot_level": slot,
    })
    return base


def build_concentration_effect(
    *,
    effect_id: str,
    mutation_id: str,
    target_kind: TargetKind,
    target_id: str,
    op: ConcentrationOp,
    effect_name: str | None = None,
    concentration_effect_id: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    visibility: str = "dm_private",
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Typed staged-effect record for concentration start/replace/break."""
    if op not in ("start", "replace", "break"):
        raise StateError("invalid_concentration_op", f"unknown concentration op {op!r}", field="op")
    if op in ("start", "replace"):
        for label, value in (("effect_name", effect_name), ("concentration_effect_id", concentration_effect_id), ("source", source)):
            if not isinstance(value, str) or not value.strip():
                raise StateError("missing_concentration_effect", f"concentration {op} requires {label}", field="concentration")
    if op == "break" and reason not in _CONCENTRATION_BREAK_REASONS:
        raise StateError("invalid_concentration_reason", f"unknown break reason {reason!r}", field="reason")
    if provenance is not None and not isinstance(provenance, dict):
        raise StateError("invalid_provenance", "provenance must be an object", field="provenance")
    base = _base_effect(effect_id=effect_id, mutation_id=mutation_id, target_kind=target_kind, target_id=target_id, visibility=visibility)
    base["effect_type"] = APPLY_CONCENTRATION_EFFECT
    base["arguments"].update({
        "op": op,
        "effect_name": effect_name,
        "concentration_effect_id": concentration_effect_id,
        "source": source,
        "reason": reason,
        "provenance": provenance,
    })
    return base


def build_death_save_effect(
    *,
    effect_id: str,
    mutation_id: str,
    target_kind: TargetKind,
    target_id: str,
    op: DeathSaveOp,
    result: DeathSaveResult | None = None,
    reset_reason: str | None = None,
    visibility: str = "dm_private",
) -> dict[str, Any]:
    """Typed staged-effect record for death-save record/reset."""
    if op not in ("record", "reset"):
        raise StateError("invalid_death_save_op", f"unknown death-save op {op!r}", field="op")
    if op == "record" and result not in ("success", "failure", "critical_success", "critical_failure"):
        raise StateError("invalid_death_result", f"unknown death-save result {result!r}", field="result")
    if op == "reset" and reset_reason not in ("healed", "stabilized", "rest", "revived"):
        raise StateError("invalid_death_reset_reason", f"unknown reset reason {reset_reason!r}", field="reset_reason")
    base = _base_effect(effect_id=effect_id, mutation_id=mutation_id, target_kind=target_kind, target_id=target_id, visibility=visibility)
    base["effect_type"] = APPLY_DEATH_SAVE_EFFECT
    base["arguments"].update({"op": op, "result": result, "reset_reason": reset_reason})
    return base


# ── Domain-event builders ─────────────────────────────────────────────────


def _event_visibility(visibility: str, *, include_private: bool) -> str:
    if include_private:
        return "dm_private"
    return "public" if visibility in ("public", "campaign", "party_known") else "dm_private"


def condition_domain_event(
    change: dict[str, Any],
    *,
    include_private: bool,
    disclosed: bool = False,
) -> tuple[str, dict[str, Any], str]:
    """(event_type, payload, visibility) triple for commit_campaign_mutation.

    A private payload always pairs with ``dm_private`` visibility. The public
    projection names the condition only when ``disclosed`` (shared-table
    state); hidden NPC specifics stay DM-only while the fact of a change is
    still recorded.
    """
    visibility = _event_visibility(str(change.get("visibility") or "dm_private"), include_private=include_private)
    if include_private:
        return (CONDITION_CHANGED_EVENT, {**change, "rules_revision": RULES_REVISION, "state_version": STATE_VERSION}, "dm_private")
    public: dict[str, Any] = {
        "target_kind": change.get("target_kind"),
        "target_id": change.get("target_id"),
        "op": change.get("op"),
        "mutation_id": change.get("mutation_id"),
    }
    if disclosed:
        public["condition"] = change.get("condition")
        if change.get("expired"):
            public["expired"] = change["expired"]
    else:
        public["redacted"] = True
    return (CONDITION_CHANGED_EVENT, public, visibility)


def resource_domain_event(
    change: dict[str, Any],
    *,
    include_private: bool,
    disclosed: bool = False,
) -> tuple[str, dict[str, Any], str]:
    visibility = _event_visibility(str(change.get("visibility") or "dm_private"), include_private=include_private)
    if include_private:
        return (RESOURCE_CHANGED_EVENT, {**change, "rules_revision": RULES_REVISION, "state_version": STATE_VERSION}, "dm_private")
    public: dict[str, Any] = {
        "target_kind": change.get("target_kind"),
        "target_id": change.get("target_id"),
        "op": change.get("op"),
        "mutation_id": change.get("mutation_id"),
    }
    if disclosed:
        public["resource"] = change.get("resource")
        public["delta"] = change.get("delta")
    else:
        public["redacted"] = True
    return (RESOURCE_CHANGED_EVENT, public, visibility)


def concentration_domain_event(
    change: dict[str, Any],
    *,
    include_private: bool,
    disclosed: bool = False,
) -> tuple[str, dict[str, Any], str]:
    visibility = _event_visibility(str(change.get("visibility") or "dm_private"), include_private=include_private)
    if include_private:
        return (CONCENTRATION_CHANGED_EVENT, {**change, "rules_revision": RULES_REVISION, "state_version": STATE_VERSION}, "dm_private")
    public: dict[str, Any] = {
        "target_kind": change.get("target_kind"),
        "target_id": change.get("target_id"),
        "op": change.get("op"),
        "mutation_id": change.get("mutation_id"),
        "active": change.get("active"),
    }
    if disclosed:
        public["effect_name"] = change.get("effect_name")
        if change.get("broke"):
            public["broke"] = change["broke"]
    else:
        public["redacted"] = True
    return (CONCENTRATION_CHANGED_EVENT, public, visibility)


def death_save_domain_event(
    change: dict[str, Any],
    *,
    include_private: bool,
    disclosed: bool = False,
) -> tuple[str, dict[str, Any], str]:
    visibility = _event_visibility(str(change.get("visibility") or "dm_private"), include_private=include_private)
    if include_private:
        return (DEATH_SAVE_CHANGED_EVENT, {**change, "rules_revision": RULES_REVISION, "state_version": STATE_VERSION}, "dm_private")
    public: dict[str, Any] = {
        "target_kind": change.get("target_kind"),
        "target_id": change.get("target_id"),
        "op": change.get("op"),
        "mutation_id": change.get("mutation_id"),
        "outcome": change.get("outcome"),
    }
    if disclosed:
        public["successes"] = change.get("successes")
        public["failures"] = change.get("failures")
    else:
        public["redacted"] = True
    return (DEATH_SAVE_CHANGED_EVENT, public, visibility)


# ── Duplicate-application guard ───────────────────────────────────────────


class StateLedger:
    """Process-local guard so one logical mutation cannot apply twice in-process.

    Same scope warning as #225/#226 ledgers: in-memory only — for crash-safe
    / multi-worker idempotency use :func:`apply_state_consequence`.
    """

    def __init__(self) -> None:
        self._applied: set[str] = set()

    def apply(self, mutation_id: str) -> Literal["applied", "duplicate"]:
        if mutation_id in self._applied:
            try:
                structured_log(logger, logging.INFO, "rules_state_duplicate_hit", mutation_id=mutation_id, scope="process")
            except Exception:
                pass
            return "duplicate"
        self._applied.add(mutation_id)
        return "applied"

    def is_duplicate(self, mutation_id: str) -> bool:
        return mutation_id in self._applied

    def __len__(self) -> int:
        return len(self._applied)


def apply_state_consequence(
    db: Any,
    *,
    actor_id: Any,
    scope_type: str,
    scope_id: Any,
    mutation_id: str,
    payload: dict[str, Any],
    execute: Callable[[], dict | list],
    command_type: str = "rules_state.apply",
) -> tuple[dict | list, bool]:
    """Durably apply one logical state mutation exactly once.

    Keyed by the stable ``mutation_id`` (caller-minted on first attempt,
    reused on retry) via the existing ``app.idempotency`` durable command
    record — no new persistence mechanism. Returns ``(result, replayed)``.
    """
    from app.idempotency import execute_idempotent_command

    t0 = time.monotonic()
    try:
        result, replayed = execute_idempotent_command(
            db,
            actor_id=actor_id,
            idempotency_key=_require_mutation_id(mutation_id),
            command_type=command_type,
            scope_type=scope_type,
            scope_id=scope_id,
            payload=payload,
            execute=execute,
        )
    finally:
        try:
            structured_log(
                logger, logging.INFO, "rules_state_idempotent_apply",
                mutation_id=mutation_id, command_type=command_type,
                latency_ms=round((time.monotonic() - t0) * 1000, 2),
                state_version=STATE_VERSION,
            )
        except Exception:
            pass
    if replayed:
        try:
            structured_log(logger, logging.INFO, "rules_state_duplicate_hit", mutation_id=mutation_id, scope="durable")
        except Exception:
            pass
    return result, replayed
