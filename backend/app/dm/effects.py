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

import hashlib
import logging
from typing import Callable, Any

from sqlalchemy.orm import Session

from models.campaigns import Campaign
from models.dm import DmTurn
from models.dm import DmTurnAttempt

logger = logging.getLogger(__name__)

# Visibility ordering for broadening check (least -> most permissive)
_VISIBILITY_ORDER = {"dm_private": 0, "party_known": 1, "public": 2}

# Effects without explicit visibility are treated as public patches (must not be promoted from private attempts).
# Relation/fact assertions default to dm_only (fail-closed): a bare claim stays
# restricted unless the contract explicitly widens it (issue #210).
_EFFECT_DEFAULT_VISIBILITY: dict[str, str] = {
    "update_scene": "public",
    "propose_sheet_update": "public",
    "assert_fact": "dm_private",
    "upsert_relation": "dm_private",
    # Encounter selection references canonical identities only; stat
    # resolution is server-side, so announcing combat is party-visible.
    "start_encounter": "public",
}

def _is_shared_audience(audience: str) -> bool:
    return (audience or "campaign") == "campaign"

def _visibility_of(effect: dict[str, Any]) -> str | None:
    args = effect.get("arguments") or {}
    return args.get("visibility")

# World-record visibility vocabulary (issues #209/#210) maps onto the
# staged-effect broadening check without widening disclosure: restricted
# stays restricted, member-visible stays member-visible.
_EFFECT_VISIBILITY_ALIASES = {"dm_only": "dm_private", "campaign": "party_known", "private": "dm_private"}

def _effective_visibility(effect: dict[str, Any]) -> str:
    vis = _visibility_of(effect)
    if vis is not None:
        return _EFFECT_VISIBILITY_ALIASES.get(str(vis), str(vis))
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
    """Apply a bounded scene patch to the authoritative current-scene row.

    Runs inside the turn-commit revision transaction (issue #209): the outer
    ``commit_campaign_mutation`` owns commit/rollback, so a failed turn
    commit leaves no half-applied scene. ``new_revision`` is the resulting
    campaign revision (prior + 1), keeping scene changes in revision order.
    """
    from app.world.service import UNSET, apply_scene_update_inline

    args = effect.get("arguments") or {}
    patch = args.get("scene_patch") or {}
    if not isinstance(patch, dict):
        raise ValueError(f"Staged effect {effect.get('id')!r} scene_patch must be an object")
    prior = int(campaign.revision) if campaign.revision is not None else 0
    # Key-presence (not truthiness) patch semantics: an explicit empty list /
    # dict / string clears state, while an absent key leaves it unchanged.
    # `patch.get("x") or patch.get("alias")` would treat [] / {} / "" as
    # absent and silently keep stale state.
    def _pick(primary: str, alias: str):
        if primary in patch:
            return patch[primary]
        if alias in patch:
            return patch[alias]
        return None
    if "present_actors" in patch:
        present_actors = patch["present_actors"]
    elif "present_actor_names" in patch:
        present_actors = patch["present_actor_names"]
    else:
        present_actors = None
    if "environment" in patch:
        environment = patch["environment"]
    elif "state" in patch:
        environment = patch["state"]
    else:
        environment = None
    apply_scene_update_inline(
        db, campaign, new_revision=prior + 1,
        # Key-presence: explicit null clears the canonical location reference
        # while omission preserves it (same as actors/environment above).
        location_entity_id=patch["location_entity_id"] if "location_entity_id" in patch else UNSET,
        location_name=_pick("location_name", "location"),
        fictional_time=_pick("fictional_time", "time"),
        fictional_time_details=patch.get("fictional_time_details"),
        present_actors=present_actors,
        environment=environment,
        visibility=args.get("visibility") or patch.get("visibility"),
        source_turn_id=turn.id, source_attempt_id=attempt.id,
        operation_id=getattr(attempt, "commit_operation_id", None) or str(attempt.id),
    )
    logger.info("effect update_scene effect_id=%s reason=%s", effect.get("id"), args.get("reason"))


@register("reveal_fact")
def _handle_reveal_fact(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    args = effect.get("arguments") or {}
    logger.info("effect reveal_fact effect_id=%s item_type=%s item_id=%s visibility=%s", effect.get("id"), args.get("item_type"), args.get("item_id"), args.get("visibility"))


@register("assert_fact")
def _handle_assert_fact(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Assert or supersede one durable epistemic fact inside the turn-commit txn.

    Runs inside the outer ``commit_campaign_mutation`` (issue #210): failed
    commits roll back both the version insert and any prior lifecycle flip,
    so failed updates never partially supersede prior active truth.
    """
    from app.world.knowledge import create_fact_inline, supersede_fact_inline

    args = effect.get("arguments") or {}
    operation_id = getattr(attempt, "commit_operation_id", None) or str(attempt.id)
    idempotency_key = _resolve_effect_key(attempt, effect)
    supersedes = args.get("supersedes_fact_id")
    if supersedes:
        supersede_fact_inline(
            db, campaign, supersedes,
            content=args.get("content"),
            entity_refs=args.get("entity_refs"),
            epistemic_state=args.get("epistemic_state"),
            visibility=args.get("visibility"),
            provenance=args.get("provenance"),
            source_turn_id=turn.id, source_attempt_id=attempt.id,
            operation_id=operation_id, idempotency_key=idempotency_key,
        )
    else:
        create_fact_inline(
            db, campaign, content=args.get("content") or "",
            entity_refs=args.get("entity_refs"),
            epistemic_state=args.get("epistemic_state") or "claimed",
            visibility=args.get("visibility"),
            provenance=args.get("provenance"),
            source_turn_id=turn.id, source_attempt_id=attempt.id,
            operation_id=operation_id, idempotency_key=idempotency_key,
        )
    logger.info("effect assert_fact effect_id=%s epistemic=%s supersedes=%s", effect.get("id"), args.get("epistemic_state"), supersedes)


@register("upsert_relation")
def _handle_upsert_relation(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Create or supersede one durable world relation inside the turn-commit txn."""
    from app.world.knowledge import create_relation_inline, supersede_relation_inline

    args = effect.get("arguments") or {}
    operation_id = getattr(attempt, "commit_operation_id", None) or str(attempt.id)
    idempotency_key = _resolve_effect_key(attempt, effect)
    supersedes = args.get("supersedes_relation_id")
    if supersedes:
        # Key-presence: absent object keys inherit the prior reference;
        # explicit null clears it (clear_object clears both sides at once).
        from app.world.service import UNSET as _UNSET
        supersede_relation_inline(
            db, campaign, supersedes,
            subject_entity_id=args.get("subject_entity_id"),
            relation_type=args.get("relation_type"),
            object_entity_id=args["object_entity_id"] if "object_entity_id" in args else _UNSET,
            object_label=args["object_label"] if "object_label" in args else _UNSET,
            epistemic_state=args.get("epistemic_state"),
            visibility=args.get("visibility"),
            provenance=args.get("provenance"),
            clear_object=bool(args.get("clear_object", False)),
            source_turn_id=turn.id, source_attempt_id=attempt.id,
            operation_id=operation_id, idempotency_key=idempotency_key,
        )
    else:
        create_relation_inline(
            db, campaign,
            subject_entity_id=args.get("subject_entity_id"),
            relation_type=args.get("relation_type") or "",
            object_entity_id=args.get("object_entity_id"),
            object_label=args.get("object_label"),
            epistemic_state=args.get("epistemic_state") or "claimed",
            visibility=args.get("visibility"),
            provenance=args.get("provenance"),
            source_turn_id=turn.id, source_attempt_id=attempt.id,
            operation_id=operation_id, idempotency_key=idempotency_key,
        )
    logger.info("effect upsert_relation effect_id=%s type=%s supersedes=%s", effect.get("id"), args.get("relation_type"), supersedes)


@register("propose_sheet_update")
def _handle_propose_sheet_update(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    args = effect.get("arguments") or {}
    logger.info("effect propose_sheet_update effect_id=%s character_id=%s changes=%s", effect.get("id"), args.get("character_id"), len(args.get("changes") or []))


@register("complete_adventure")
def _handle_complete_adventure(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Close the campaign's adventure arc inside the turn-commit txn (issue #260).

    Runs inside the outer ``commit_campaign_mutation``: a failed turn commit
    rolls back the adventure close, so failed completions leave the adventure
    open rather than half-complete. The campaign status is untouched — it
    stays active and later adventures can be created in the same world.

    Duplicate protection: a retried effect with the same idempotency key
    against an already-completed adventure is a no-op; a genuinely new
    completion against an already-closed adventure fails closed.
    """
    import uuid as _uuid

    from app.adventures.service import (
        ADVENTURE_CLOSING_JOB,
        complete_adventure_inline,
        find_by_operation,
        get_current_adventure,
    )
    from models.campaigns import Adventure

    args = effect.get("arguments") or {}
    operation_key = _resolve_effect_key(attempt, effect)

    adventure: Adventure | None = None
    raw_aid = str(args.get("adventure_id") or "").strip()
    if raw_aid:
        try:
            adventure = db.get(Adventure, _uuid.UUID(raw_aid))
        except ValueError:
            raise ValueError(f"Staged effect {effect.get('id')!r} adventure_id must be a UUID")
        if adventure is None or str(adventure.campaign_id) != str(campaign.id):
            raise ValueError(f"Staged effect {effect.get('id')!r} adventure not found in this campaign")
    else:
        adventure = get_current_adventure(db, campaign.id)
        if adventure is None:
            # Idempotent replay: the original commit closed the adventure and
            # stored this effect's operation key on it.
            replay = find_by_operation(db, campaign.id, operation_key)
            if replay is not None and replay.status == "completed":
                logger.info(
                    "effect complete_adventure duplicate_replay effect_id=%s adventure_id=%s op=%s",
                    effect.get("id"), replay.id, operation_key,
                )
                return
            raise ValueError(
                f"Staged effect {effect.get('id')!r} has no active adventure to complete in campaign {campaign.id}"
            )

    if adventure.status == "completed":
        if adventure.operation_id == operation_key:
            logger.info(
                "effect complete_adventure duplicate effect_id=%s adventure_id=%s op=%s",
                effect.get("id"), adventure.id, operation_key,
            )
            return
        raise ValueError(
            f"Staged effect {effect.get('id')!r} adventure {adventure.id} is already completed "
            f"(outcome={adventure.outcome})"
        )

    complete_adventure_inline(
        db, campaign, adventure,
        outcome=args.get("outcome"),
        reason=args.get("reason"),
        public_summary=args.get("public_summary"),
        source_turn_id=turn.id,
        operation_id=operation_key,
    )

    # Enqueue downstream closing work (recap/rewards) in the same transaction:
    # best-effort, never invalidates the committed completion.
    from app.observability.tracing import current_trace_id
    from models.reliability import Outbox as _Outbox

    db.add(_Outbox(
        id=_uuid.uuid4(),
        aggregate_type="campaign",
        aggregate_id=campaign.id,
        campaign_id=campaign.id,
        event_type=ADVENTURE_CLOSING_JOB,
        operation_id=operation_key,
        trace_id=current_trace_id(),
        payload={
            "adventure_id": str(adventure.id),
            "campaign_id": str(campaign.id),
            "outcome": adventure.outcome,
            "operation_id": operation_key,
        },
        status="pending",
        attempts=0,
    ))
    # Derived summary finalization happens post-commit in the turn commit
    # path (issue #263): the authoritative completion event/revision only
    # exists after commit_campaign_mutation returns, so binding the end
    # cursor here would permanently truncate the range by one step.
    db.flush()
    logger.info(
        "effect complete_adventure effect_id=%s adventure_id=%s outcome=%s op=%s",
        effect.get("id"), adventure.id, adventure.outcome, operation_key,
    )


@register("start_encounter")
def _handle_start_encounter(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Start an authoritative encounter inside the turn-commit txn (issue #230).

    Runs inside the outer ``commit_campaign_mutation``: a failed turn commit
    rolls back the encounter rows, participants, NPC rolls, and pending human
    initiative requests, so failed starts leave no half-created combat.

    Duplicate protection: a retried effect with the same idempotency key
    against an already-started encounter is a no-op returning the existing
    row; a genuinely new start while one is pending/active fails closed.
    """
    from app.combat.service import start_encounter_inline

    args = effect.get("arguments") or {}
    operation_key = _resolve_effect_key(attempt, effect)
    encounter = start_encounter_inline(db, campaign, turn, attempt, args, operation_key)
    logger.info(
        "effect start_encounter effect_id=%s encounter_id=%s participants=%s op=%s",
        effect.get("id"), encounter.id, encounter.participant_count, operation_key,
    )


def _default_effect_key(attempt: DmTurnAttempt, effect: dict[str, Any]) -> str:
    """Bounded collision-resistant default idempotency key for one staged effect.

    Keys derive from the attempt UUID (36 chars) plus the effect ID, so the
    effect identity can never be truncated away — unlike slicing a
    ``commit_operation_id``-prefixed composite back to 128 chars, which
    collapses distinct same-type effects to one key when the operation ID is
    long and silently drops later writes as false duplicates. Overlong
    composites (unvalidated effect IDs) fall back to a sha256 namespace.
    Explicit caller-supplied keys are never rewritten by this helper.
    """
    effect_id = str(effect.get("id") or "unknown")
    base = f"{attempt.id}:{effect_id}"
    if len(base) <= 128:
        return base
    return f"eff:{hashlib.sha256(base.encode('utf-8')).hexdigest()}"


def _scoped_effect_key(attempt: DmTurnAttempt, key: str) -> str:
    """Scope an explicit staged-effect key to its attempt.

    The durable uniqueness scope is campaign-wide, so a bare explicit key
    could collide with an older turn's key: the later write would return the
    older row as a duplicate and the turn would commit without storing the
    new record.     Prefixing with the attempt UUID keeps same-attempt retries
    idempotent (matches the ``jit:{attempt_id}:{temp_id}`` convention from
    #209) while preventing cross-turn aliasing. The ``x:`` domain tag keeps
    this namespace disjoint from generated ``{attempt.id}:{effect_id}`` keys
    so an explicit key equal to another effect's ID can never alias it.
    Overlong composites fall back to a deterministic sha256 namespace so
    retries stay stable.
    """
    base = f"x:{attempt.id}:{key}"
    if len(base) <= 128:
        return base
    return f"eff:{hashlib.sha256(base.encode('utf-8')).hexdigest()}"


def _resolve_effect_key(attempt: DmTurnAttempt, effect: dict[str, Any]) -> str:
    """Durable idempotency key for one staged knowledge effect.

    Explicit caller keys are honored but attempt-scoped; otherwise the
    generated attempt/effect namespace applies. Both branches stay within
    the 128-char durable key bound.
    """
    args = effect.get("arguments") or {}
    explicit = str(args.get("idempotency_key") or "").strip()
    if explicit:
        return _scoped_effect_key(attempt, explicit)
    return _default_effect_key(attempt, effect)


def list_registered_effect_types() -> list[str]:
    return sorted(_REGISTRY.keys())
