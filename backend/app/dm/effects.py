"""Staged effect registry — issue #206.

Typed, non-generic effect application. Effects remain attempt-local until
atomic commit via commit_turn_with_effects; this registry is the extensible
promotion point.

Handlers are intentionally narrow: they receive (db, campaign, effect_dict, turn, attempt)
and must not broaden visibility. Unknown types fail closed.

Security: staged effects retain audience/visibility metadata and cannot broaden
disclosure when promoted.
"""

from __future__ import annotations

import logging
from typing import Callable, Any

from sqlalchemy.orm import Session

from models.campaigns import Campaign
from models.dm import DmTurn
from models.dm import DmTurnAttempt

logger = logging.getLogger(__name__)

# Visibility ordering for broadening check (least -> most permissive)
_VISIBILITY_ORDER = {"dm_private": 0, "party_known": 1, "public": 2}

# Effects without explicit visibility are treated as public patches (must not be promoted from private attempts)
_EFFECT_DEFAULT_VISIBILITY: dict[str, str] = {
    "update_scene": "public",
    "propose_sheet_update": "public",
}

def _is_shared_audience(audience: str) -> bool:
    return (audience or "campaign") == "campaign"

def _visibility_of(effect: dict[str, Any]) -> str | None:
    args = effect.get("arguments") or {}
    return args.get("visibility")

def _effective_visibility(effect: dict[str, Any]) -> str:
    vis = _visibility_of(effect)
    if vis is not None:
        return vis
    eff_type = effect.get("effect_type")
    return _EFFECT_DEFAULT_VISIBILITY.get(eff_type, "public")

def _assert_visibility_not_broadened(effect: dict[str, Any], attempt_audience: str):
    """Fail closed if promotion would broaden staged-effect visibility beyond attempt audience.

    - Shared (campaign) attempts may host any effect visibility; private stays private via projection.
    - Private attempts may only promote dm_private effects. A public/party_known effect (or
      visibility-less type treated as public) from a private attempt would leak private
      context to a wider audience and is rejected at commit (issue #206).
    """
    effective = _effective_visibility(effect)
    if effective not in _VISIBILITY_ORDER:
        raise ValueError(f"Unknown visibility {effective!r} on staged effect {effect.get('id')}")
    if _is_shared_audience(attempt_audience):
        return
    # Private attempt: only dm_private is non-broadening
    if effective != "dm_private":
        raise ValueError(
            f"Staged effect {effect.get('id')!r} type {effect.get('effect_type')!r} effective visibility {effective!r} "
            f"would broaden private attempt audience {attempt_audience!r} — only dm_private allowed"
        )

EffectHandler = Callable[[Session, Campaign, dict[str, Any], DmTurn, DmTurnAttempt], None]
_REGISTRY: dict[str, EffectHandler] = {}


def register(effect_type: str):
    """Decorator to register a handler for an effect_type."""
    def deco(fn: EffectHandler):
        if effect_type in _REGISTRY:
            raise ValueError(f"Handler already registered for {effect_type}")
        _REGISTRY[effect_type] = fn
        return fn
    return deco


def apply_staged_effects(
    db: Session,
    campaign: Campaign,
    staged_effects: list[dict[str, Any]],
    turn: DmTurn,
    attempt: DmTurnAttempt,
) -> None:
    """Dispatch each staged effect through its registered handler.

    Raises on unknown effect_type or visibility broadening (fail-closed).
    Called inside commit_campaign_mutation's mutate callback, so any exception
    rolls back the entire commit (all-or-nothing).
    """
    if not staged_effects:
        logger.info("staged_effects apply none turn_id=%s attempt_id=%s", turn.id, attempt.id)
        return

    logger.info(
        "staged_effects apply start turn_id=%s attempt_id=%s count=%s types=%s",
        turn.id, attempt.id, len(staged_effects), [e.get("effect_type") for e in staged_effects],
    )

    for eff in staged_effects:
        eff_id = eff.get("id", "<unknown>")
        eff_type = eff.get("effect_type")
        if eff_type not in _REGISTRY:
            raise ValueError(f"Unknown staged effect_type {eff_type!r} for effect {eff_id!r} — no handler registered")
        _assert_visibility_not_broadened(eff, attempt.audience)
        handler = _REGISTRY[eff_type]
        handler(db, campaign, eff, turn, attempt)
        logger.info("staged_effect applied turn_id=%s attempt_id=%s effect_id=%s effect_type=%s", turn.id, attempt.id, eff_id, eff_type)

    logger.info("staged_effects apply complete turn_id=%s attempt_id=%s count=%s", turn.id, attempt.id, len(staged_effects))


# ── Built-in handlers (stubs, extensible) ────────────────────────────────────

@register("record_world_event")
def _handle_record_world_event(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    # World events are recorded as part of the domain event payload; no extra mutation needed.
    # Handler exists to enforce validation and future extension (e.g., write to world_events table).
    args = effect.get("arguments") or {}
    # Validate visibility retained (no broadening inside handler)
    # No DB mutation here beyond logging; commit payload already captures it.
    logger.info("effect record_world_event effect_id=%s summary=%s visibility=%s", effect.get("id"), args.get("summary"), args.get("visibility"))


@register("update_scene")
def _handle_update_scene(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    args = effect.get("arguments") or {}
    # Stub: scene patches would apply to a current_scene table; for now no-op but validated.
    logger.info("effect update_scene effect_id=%s reason=%s", effect.get("id"), args.get("reason"))


@register("reveal_fact")
def _handle_reveal_fact(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    args = effect.get("arguments") or {}
    logger.info("effect reveal_fact effect_id=%s item_type=%s item_id=%s visibility=%s", effect.get("id"), args.get("item_type"), args.get("item_id"), args.get("visibility"))


@register("propose_sheet_update")
def _handle_propose_sheet_update(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    args = effect.get("arguments") or {}
    logger.info("effect propose_sheet_update effect_id=%s character_id=%s changes=%s", effect.get("id"), args.get("character_id"), len(args.get("changes") or []))


def list_registered_effect_types() -> list[str]:
    return sorted(_REGISTRY.keys())
