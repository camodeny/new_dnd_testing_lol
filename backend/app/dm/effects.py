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
    # Encounter end (#239) defaults to dm_private (fail-closed): the
    # DM-authored reason may reference hidden NPC fates, and the
    # thread-scoped ended event + snapshot enforce their own read boundary.
    "end_encounter": "dm_private",
    # Attack damage defaults to dm_private (fail-closed): the builder sets
    # explicit visibility per target, and a bare effect stays restricted.
    "apply_attack_damage": "dm_private",
    # Rules-state transitions (#227) are code-built only and default to
    # dm_private (fail-closed): hidden NPC conditions/resources stay DM-only
    # unless the builder explicitly widens them for shared-table state.
    "apply_condition": "dm_private",
    "apply_resource": "dm_private",
    "apply_concentration": "dm_private",
    "apply_death_save": "dm_private",
    # DM-authored map terrain (#232) defaults to dm_private (fail-closed):
    # hidden trap/stranded geometry must never widen to a shared audience
    # unless the staged effect explicitly says so.
    "update_map_terrain": "dm_private",
    # DM-authored token placements (#232) likewise default to dm_private
    # (fail-closed): repositioning hidden tokens must never widen to a
    # shared audience unless the staged effect explicitly says so.
    "update_map_placement": "dm_private",
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
    # Issue #230: start_encounter carries its own thread-scoped audience —
    # snapshot, direct encounter reads, and realtime all enforce the source
    # thread, and staged NPC mechanics are redacted for non-owners — so
    # promoting it from a private attempt cannot broaden disclosure beyond
    # that thread.
    if effect.get("effect_type") == "start_encounter":
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

    _reject_duplicate_damage_ids(staged_effects)
    _reject_duplicate_state_mutations(staged_effects)

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


def _reject_duplicate_damage_ids(staged_effects: list[dict[str, Any]]) -> None:
    """Fail closed if one logical damage record would apply twice (#226).

    Two ``apply_attack_damage`` effects with different staged-effect IDs but
    the same logical ``damage_id`` would both run and reduce HP twice in the
    same atomic commit; outer turn idempotency only protects whole-commit
    replay, not two entries inside it. The scan runs before any handler, so
    rejection leaves zero partial mutation. A missing/blank ``damage_id``
    is also rejected: the logical damage identity is required for the
    exactly-once guarantee.
    """
    seen: dict[str, str] = {}
    for eff in staged_effects:
        if eff.get("effect_type") != "apply_attack_damage":
            continue
        args = eff.get("arguments") or {}
        damage_id = args.get("damage_id")
        if not isinstance(damage_id, str) or not damage_id.strip():
            raise ValueError(
                f"Staged effect {eff.get('id')!r} apply_attack_damage requires a stable damage_id"
            )
        if damage_id in seen:
            raise ValueError(
                f"Duplicate logical damage_id {damage_id!r} in staged effects "
                f"{seen[damage_id]!r} and {eff.get('id')!r} — one logical damage effect cannot apply twice"
            )
        seen[damage_id] = str(eff.get("id"))


def _reject_duplicate_state_mutations(staged_effects: list[dict[str, Any]]) -> None:
    """Fail closed if one logical rules-state mutation would apply twice (#227).

    Two staged entries of the ``apply_condition`` / ``apply_resource`` /
    ``apply_concentration`` / ``apply_death_save`` types sharing one stable
    ``mutation_id`` would both run and double-apply (double spend, double
    condition tick) in the same atomic commit; outer turn idempotency only
    protects whole-commit replay, not two entries inside it. The scan runs
    before any handler, so rejection leaves zero partial mutation. A
    missing/blank ``mutation_id`` is also rejected: the logical mutation
    identity is required for the exactly-once guarantee.
    """
    from app.rules.state import STATE_EFFECT_TYPES as _STATE_TYPES

    seen: dict[str, str] = {}
    for eff in staged_effects:
        if eff.get("effect_type") not in _STATE_TYPES:
            continue
        args = eff.get("arguments") or {}
        mutation_id = args.get("mutation_id")
        if not isinstance(mutation_id, str) or not mutation_id.strip():
            raise ValueError(
                f"Staged effect {eff.get('id')!r} type {eff.get('effect_type')!r} requires a stable mutation_id"
            )
        if mutation_id in seen:
            raise ValueError(
                f"Duplicate logical mutation_id {mutation_id!r} in staged effects "
                f"{seen[mutation_id]!r} and {eff.get('id')!r} — one logical rules-state mutation cannot apply twice"
            )
        seen[mutation_id] = str(eff.get("id"))


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


@register("end_encounter")
def _handle_end_encounter(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """End an authoritative encounter inside the turn-commit txn (issue #239).

    Runs inside the outer ``commit_campaign_mutation``: a failed turn commit
    rolls back the end transition, death writes, and hook rows, so failed
    ends leave the encounter active rather than half-closed. The distinct
    ``encounter.ended`` lifecycle event is staged by the turn-commit path in
    the same outer transaction (mirroring the start_encounter staging).

    Duplicate protection: a retried effect with the same idempotency key
    against the already-ended encounter is a no-op returning the existing
    row; a genuinely new end against an ended encounter fails closed.
    """
    import uuid as _uuid

    from models.combat import Encounter as _Encounter

    args = effect.get("arguments") or {}
    try:
        encounter_id = _uuid.UUID(str(args.get("encounter_id") or ""))
    except ValueError:
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter_id must be a UUID")
    encounter = db.get(_Encounter, encounter_id)
    if encounter is None or str(encounter.campaign_id) != str(campaign.id):
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter {encounter_id} not found in this campaign")
    operation_key = _resolve_effect_key(attempt, effect)

    from app.combat.ending import EndEncounterError as _EndError
    from app.combat.ending import end_encounter_inline as _end_inline

    try:
        encounter = _end_inline(db, campaign, encounter, args, operation_key)
    except _EndError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid encounter end: {exc}") from exc
    logger.info(
        "effect end_encounter effect_id=%s encounter_id=%s outcome=%s op=%s",
        effect.get("id"), encounter.id, encounter.end_outcome, operation_key,
    )


@register("update_map_terrain")
def _handle_update_map_terrain(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a DM-authored terrain change inside the turn-commit txn (issue #232).

    Routes to the combat-lane :func:`app.combat.maps.update_terrain_inline`
    (lazy import; combat code never imports dm code). Runs inside the outer
    ``commit_campaign_mutation``: a failed turn commit rolls the terrain
    write and map-revision bump back, so failed commits leave no
    half-applied geometry. Geometry legality stays code-owned in the
    combat lane — this handler only scopes the encounter to this campaign
    and converts :class:`MapError` into a fail-closed ``ValueError``.
    """
    import uuid as _uuid

    from models.combat import Encounter as _Encounter

    args = effect.get("arguments") or {}
    try:
        encounter_id = _uuid.UUID(str(args.get("encounter_id") or ""))
    except ValueError:
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter_id must be a UUID")
    encounter = db.get(_Encounter, encounter_id)
    if encounter is None or str(encounter.campaign_id) != str(campaign.id):
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter {encounter_id} not found in this campaign")
    operation_key = _resolve_effect_key(attempt, effect)

    from app.combat.maps import MapError as _MapError
    from app.combat.maps import update_terrain_inline as _update_terrain_inline

    try:
        encounter_map = _update_terrain_inline(db, campaign, encounter, args, operation_key)
    except _MapError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid terrain change: {exc}") from exc
    logger.info(
        "effect update_map_terrain effect_id=%s encounter_id=%s map_revision=%s op=%s",
        effect.get("id"), encounter.id, encounter_map.revision, operation_key,
    )


@register("update_map_placement")
def _handle_update_map_placement(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a DM-authored placement change inside the turn-commit txn (issue #232).

    Routes to the combat-lane :func:`app.combat.maps.update_placements_inline`
    (lazy import; combat code never imports dm code). Runs inside the outer
    ``commit_campaign_mutation``: a failed turn commit rolls the placement
    rewrite and map-revision bump back. Placement legality stays code-owned
    in the combat lane — this handler only scopes the encounter to this
    campaign and converts :class:`MapError` into a fail-closed ``ValueError``.
    """
    import uuid as _uuid

    from models.combat import Encounter as _Encounter

    args = effect.get("arguments") or {}
    try:
        encounter_id = _uuid.UUID(str(args.get("encounter_id") or ""))
    except ValueError:
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter_id must be a UUID")
    encounter = db.get(_Encounter, encounter_id)
    if encounter is None or str(encounter.campaign_id) != str(campaign.id):
        raise ValueError(f"Staged effect {effect.get('id')!r} encounter {encounter_id} not found in this campaign")
    operation_key = _resolve_effect_key(attempt, effect)

    from app.combat.maps import MapError as _MapError
    from app.combat.maps import update_placements_inline as _update_placements_inline

    try:
        encounter_map = _update_placements_inline(db, campaign, encounter, args, operation_key)
    except _MapError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid placement change: {exc}") from exc
    logger.info(
        "effect update_map_placement effect_id=%s encounter_id=%s map_revision=%s op=%s",
        effect.get("id"), encounter.id, encounter_map.revision, operation_key,
    )


@register("apply_attack_damage")
def _handle_apply_attack_damage(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply resolved attack damage to PC sheet HP or NPC entity HP (issue #226).

    Runs inside the outer ``commit_campaign_mutation``: a failed turn commit
    rolls back the HP write, so failed commits leave no half-applied damage.
    The damage total is already resolved deterministically before staging —
    this handler only performs code-owned HP arithmetic (temp absorbs first,
    remainder to current, floor 0) via :func:`app.rules.attacks.apply_damage`.

    Duplicate protection comes from the outer turn-commit idempotency (one
    commit per attempt/effect key); the write itself is a pure function of
    the staged total, so replaying the same staged effect converges.
    """
    import uuid as _uuid

    from sqlalchemy import select as _select

    from app.rules.attacks import AttackError as _AttackError
    from app.rules.attacks import HitPoints as _HitPoints
    from app.rules.attacks import apply_damage as _apply_damage

    args = effect.get("arguments") or {}
    target_kind = args.get("target_kind")
    if target_kind not in ("pc", "npc"):
        raise ValueError(f"Staged effect {effect.get('id')!r} target_kind must be pc/npc")
    try:
        target_id = _uuid.UUID(str(args.get("target_id") or ""))
    except ValueError:
        raise ValueError(f"Staged effect {effect.get('id')!r} target_id must be a UUID")
    try:
        total = int(args.get("damage_total"))
    except (TypeError, ValueError):
        raise ValueError(f"Staged effect {effect.get('id')!r} damage_total must be an integer")
    if total < 0:
        raise ValueError(f"Staged effect {effect.get('id')!r} damage_total must be >= 0")
    change_id = _resolve_effect_key(attempt, effect)

    if target_kind == "pc":
        from models.campaigns import CampaignMember
        from models.characters import Character, Dnd5eCharacterSheet

        character = db.get(Character, target_id)
        if character is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} character {target_id} not found")
        # Roster scoping (#266 canonical, same pattern as combat participant
        # validation): the character must be on this campaign's active roster.
        # Without this, a staged effect committed for campaign A could reduce
        # HP on an unrelated campaign/user character.
        roster = db.execute(
            _select(CampaignMember).where(
                CampaignMember.campaign_id == campaign.id,
                CampaignMember.selected_character_id == character.id,
            )
        ).scalars().first()
        if roster is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} character {target_id} is not on this campaign's active roster")
        sheet = db.execute(
            _select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == character.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        if sheet is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} has no sheet for character {target_id}")
        try:
            before = _HitPoints(current=int(sheet.hit_points_current), maximum=int(sheet.hit_points_max), temporary=int(sheet.hit_points_temp or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Staged effect {effect.get('id')!r} sheet HP is malformed: {exc}") from exc
        try:
            change = _apply_damage(before, total, change_id=change_id)
        except _AttackError as exc:
            raise ValueError(f"Staged effect {effect.get('id')!r} HP application failed: {exc}") from exc
        sheet.hit_points_current = change.after.current
        sheet.hit_points_temp = change.after.temporary
        db.flush()
        logger.info(
            "effect apply_attack_damage pc effect_id=%s character_id=%s total=%s absorbed=%s applied=%s",
            effect.get("id"), target_id, total, change.absorbed_by_temp, change.applied_to_current,
        )
        return

    from models.world import WorldEntity

    entity = db.get(WorldEntity, target_id)
    if entity is None or str(entity.campaign_id) != str(campaign.id):
        raise ValueError(f"Staged effect {effect.get('id')!r} NPC entity {target_id} not found in this campaign")
    details = dict(entity.details or {})
    nested = details.get("hit_points")
    if not isinstance(nested, dict):
        raise ValueError(f"Staged effect {effect.get('id')!r} NPC entity {target_id} has no hit_points in details")
    try:
        before = _HitPoints(
            current=int(nested.get("current", nested.get("current_hp"))),
            maximum=int(nested.get("maximum", nested.get("max_hp", nested.get("max")))),
            temporary=int(nested.get("temporary", nested.get("temp_hp", 0)) or 0),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} NPC HP is malformed: {exc}") from exc
    try:
        change = _apply_damage(before, total, change_id=change_id)
    except _AttackError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} HP application failed: {exc}") from exc
    details["hit_points"] = {"current": change.after.current, "maximum": change.after.maximum, "temporary": change.after.temporary}
    entity.details = details
    db.flush()
    logger.info(
        "effect apply_attack_damage npc effect_id=%s entity_id=%s total=%s absorbed=%s applied=%s",
        effect.get("id"), target_id, total, change.absorbed_by_temp, change.applied_to_current,
    )


# ── Rules-state targets (#227) ────────────────────────────────────────────
#
# Canonical-store adapter: PCs mutate Dnd5eCharacterSheet columns/JSONB in
# place; NPCs mutate WorldEntity.details keys. Both shapes use the same
# condition/resource/slot/concentration dict vocabulary as
# :mod:`app.rules.state`, so the pure transitions never branch on kind.


class _StateTarget:
    """Mutable view of one combatant's rules state with a single commit."""

    def __init__(self, *, kind: str, row: Any, campaign_id: Any):
        self.kind = kind
        self.row = row
        self.campaign_id = campaign_id
        if kind == "pc":
            self.conditions: list[dict[str, Any]] = [dict(i) for i in (row.conditions or []) if isinstance(i, dict)]
            self.resources: list[dict[str, Any]] = [dict(i) for i in (row.resources or []) if isinstance(i, dict)]
            self.slots: dict[str, Any] = dict(row.spell_slots or {})
            extras = dict(row.extras or {})
            self._extras = extras
            self.concentration: dict[str, Any] | None = extras.get("concentration")
            self.successes = int(row.death_save_successes or 0)
            self.failures = int(row.death_save_failures or 0)
            self.hp_current: int | None = int(row.hit_points_current)
            self.exhaustion: int = int(row.exhaustion_level or 0)
        else:
            details = dict(row.details or {})
            self._details = details
            self.conditions = [dict(i) for i in (details.get("conditions") or []) if isinstance(i, dict)]
            self.resources = [dict(i) for i in (details.get("resources") or []) if isinstance(i, dict)]
            self.slots = dict(details.get("spell_slots") or {})
            self.concentration = details.get("concentration")
            death = details.get("death_saves") or {}
            self.successes = int(death.get("successes", 0) or 0)
            self.failures = int(death.get("failures", 0) or 0)
            nested_hp = details.get("hit_points")
            self.hp_current = int(nested_hp.get("current")) if isinstance(nested_hp, dict) and nested_hp.get("current") is not None else None
            self.exhaustion = int(details.get("exhaustion_level", 0) or 0)

    def commit(self, db: Session) -> None:
        if self.kind == "pc":
            self.row.conditions = self.conditions
            self.row.resources = self.resources
            self.row.spell_slots = self.slots
            extras = dict(self._extras)
            extras["concentration"] = self.concentration
            self.row.extras = extras
            self.row.death_save_successes = self.successes
            self.row.death_save_failures = self.failures
            if self.hp_current is not None:
                self.row.hit_points_current = self.hp_current
            self.row.exhaustion_level = self.exhaustion
        else:
            details = dict(self._details)
            details["conditions"] = self.conditions
            details["resources"] = self.resources
            details["spell_slots"] = self.slots
            details["concentration"] = self.concentration
            # Scalar death-save counters carry no per-field visibility lane;
            # DM-only disclosure is tracked in details["rules_state_visibility"]
            # via _mark_npc_section() so projections can redact without
            # inventing a second counter shape.
            details["death_saves"] = {"successes": self.successes, "failures": self.failures}
            if self.hp_current is not None:
                nested = dict(details.get("hit_points") or {})
                nested["current"] = self.hp_current
                details["hit_points"] = nested
            details["exhaustion_level"] = self.exhaustion
            self.row.details = details
        db.flush()


def _mark_npc_section(target: _StateTarget, section: str, visibility: Any) -> None:
    """Preserve staged visibility in NPC marker map (no-op for PCs)."""
    if target.kind != "npc":
        return
    from app.rules.state import mark_npc_section_visibility as _mark

    _mark(target._details, section, visibility)


def _load_state_target(db: Session, campaign: Campaign, effect: dict[str, Any]) -> _StateTarget:
    """Load + scope-check the PC sheet or NPC entity for a rules-state effect."""
    import uuid as _uuid

    from sqlalchemy import select as _select

    args = effect.get("arguments") or {}
    target_kind = args.get("target_kind")
    if target_kind not in ("pc", "npc"):
        raise ValueError(f"Staged effect {effect.get('id')!r} target_kind must be pc/npc")
    try:
        target_id = _uuid.UUID(str(args.get("target_id") or ""))
    except ValueError:
        raise ValueError(f"Staged effect {effect.get('id')!r} target_id must be a UUID")

    if target_kind == "pc":
        from models.campaigns import CampaignMember
        from models.characters import Character, Dnd5eCharacterSheet

        character = db.get(Character, target_id)
        if character is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} character {target_id} not found")
        roster = db.execute(
            _select(CampaignMember).where(
                CampaignMember.campaign_id == campaign.id,
                CampaignMember.selected_character_id == character.id,
            )
        ).scalars().first()
        if roster is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} character {target_id} is not on this campaign's active roster")
        sheet = db.execute(
            _select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == character.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        if sheet is None:
            raise ValueError(f"Staged effect {effect.get('id')!r} has no sheet for character {target_id}")
        return _StateTarget(kind="pc", row=sheet, campaign_id=campaign.id)

    from models.world import WorldEntity

    entity = db.get(WorldEntity, target_id)
    if entity is None or str(entity.campaign_id) != str(campaign.id):
        raise ValueError(f"Staged effect {effect.get('id')!r} NPC entity {target_id} not found in this campaign")
    return _StateTarget(kind="npc", row=entity, campaign_id=campaign.id)


@register("apply_condition")
def _handle_apply_condition(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a condition add/remove/update/tick to canonical state (issue #227).

    Runs inside the outer ``commit_campaign_mutation``: a failed turn commit
    rolls back the write. Gaining an incapacitating condition deterministically
    breaks active concentration (2024 linkage hook); exhaustion transitions
    sync the exhaustion column and the structural entry together.
    """
    from app.rules.state import (
        CONCENTRATION_BREAKING_CONDITIONS as _BREAKING,
    )
    from app.rules.state import (
        StateError as _StateError,
    )
    from app.rules.state import (
        add_condition as _add,
    )
    from app.rules.state import (
        break_concentration as _break_conc,
    )
    from app.rules.state import (
        normalize_condition_name as _norm,
    )
    from app.rules.state import (
        remove_condition as _remove,
    )
    from app.rules.state import (
        set_exhaustion as _set_exhaustion,
    )
    from app.rules.state import (
        tick_conditions as _tick,
    )
    from app.rules.state import (
        update_condition as _update,
    )

    args = effect.get("arguments") or {}
    op = args.get("op")
    mutation_id = args.get("mutation_id")
    target = _load_state_target(db, campaign, effect)
    # Preserve staged visibility in persisted NPC state: builders default to
    # dm_private (fail-closed) so a hidden mutation never persists as public.
    staged_visibility = args.get("visibility") or "dm_private"
    concentration_broken: str | None = None
    try:
        if op == "add":
            norm = _norm(args.get("condition"))
            new_conditions, _record = _add(
                target.conditions,
                name=norm,
                source=args.get("source"),
                duration_rounds=args.get("duration_rounds"),
                save_ends=args.get("save_ends"),
                is_permanent=bool(args.get("is_permanent", False)),
                visibility=staged_visibility,
                description=args.get("description"),
                provenance=args.get("provenance"),
                mutation_id=mutation_id,
            )
            target.conditions = new_conditions
            _mark_npc_section(target, "conditions", staged_visibility)
            if norm == "exhaustion":
                target.exhaustion = _set_exhaustion(int(args.get("exhaustion_level")), mutation_id=mutation_id)
                _mark_npc_section(target, "exhaustion_level", staged_visibility)
            if norm in _BREAKING:
                try:
                    broke_state, broke = _break_conc(target.concentration, reason="incapacitated", mutation_id=f"{mutation_id}:conc")
                    target.concentration = broke_state
                    concentration_broken = broke.effect_name
                    _mark_npc_section(target, "concentration", staged_visibility)
                except _StateError:
                    pass  # no active concentration — nothing to break
        elif op == "remove":
            norm = _norm(args.get("condition"))
            new_conditions, _removed = _remove(target.conditions, name=norm, mutation_id=mutation_id)
            target.conditions = new_conditions
            _mark_npc_section(target, "conditions", staged_visibility)
            if norm == "exhaustion":
                target.exhaustion = _set_exhaustion(0, mutation_id=mutation_id)
                _mark_npc_section(target, "exhaustion_level", staged_visibility)
        elif op == "update":
            norm = _norm(args.get("condition"))
            new_conditions, _updated = _update(
                target.conditions,
                name=norm,
                mutation_id=mutation_id,
                source=args.get("source"),
                duration_rounds=args.get("duration_rounds"),
                clear_duration=bool(args.get("clear_duration", False)),
                save_ends=args.get("save_ends"),
                clear_save_ends=bool(args.get("clear_save_ends", False)),
                is_permanent=args.get("is_permanent"),
                visibility=staged_visibility,
                description=args.get("description"),
                provenance=args.get("provenance"),
            )
            target.conditions = new_conditions
            _mark_npc_section(target, "conditions", staged_visibility)
            if norm == "exhaustion" and args.get("exhaustion_level") is not None:
                target.exhaustion = _set_exhaustion(int(args.get("exhaustion_level")), mutation_id=mutation_id)
                _mark_npc_section(target, "exhaustion_level", staged_visibility)
        elif op == "tick":
            new_conditions, _expired = _tick(target.conditions, rounds=int(args.get("rounds", 1) or 1), mutation_id=mutation_id)
            target.conditions = new_conditions
            _mark_npc_section(target, "conditions", staged_visibility)
        else:
            raise ValueError(f"Staged effect {effect.get('id')!r} unknown condition op {op!r}")
    except _StateError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid condition transition ({exc.code}): {exc}") from exc
    target.commit(db)
    logger.info(
        "effect apply_condition effect_id=%s target=%s:%s op=%s condition=%s conc_broken=%s",
        effect.get("id"), args.get("target_kind"), args.get("target_id"), op, args.get("condition"), concentration_broken,
    )


@register("apply_resource")
def _handle_apply_resource(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a resource spend/restore/set or spell-slot spend/restore (issue #227).

    Runs inside the outer ``commit_campaign_mutation``: overdrafts and
    unknown names/slots fail closed before any write, and a failed turn
    commit rolls the write back.
    """
    from app.rules.state import StateError as _StateError
    from app.rules.state import restore_resource as _restore
    from app.rules.state import restore_spell_slot as _restore_slot
    from app.rules.state import set_resource as _set
    from app.rules.state import spend_resource as _spend
    from app.rules.state import spend_spell_slot as _spend_slot

    args = effect.get("arguments") or {}
    op = args.get("op")
    mutation_id = args.get("mutation_id")
    target = _load_state_target(db, campaign, effect)
    staged_visibility = args.get("visibility") or "dm_private"
    slot_level = args.get("slot_level")
    resource = args.get("resource")
    if isinstance(resource, str) and resource.strip().lower().startswith("spell_slots:"):
        try:
            slot_level = int(resource.strip().split(":", 1)[1])
        except ValueError:
            raise ValueError(f"Staged effect {effect.get('id')!r} unparseable slot resource {resource!r}")
    try:
        if slot_level is not None:
            if op == "spend":
                new_slots, _delta = _spend_slot(target.slots, level=int(slot_level), mutation_id=mutation_id)
            elif op == "restore":
                new_slots, _delta = _restore_slot(target.slots, level=int(slot_level), amount=int(args.get("amount", 1) or 1), mutation_id=mutation_id)
            else:
                raise ValueError(f"Staged effect {effect.get('id')!r} spell slots support spend/restore only, not {op!r}")
            target.slots = new_slots
            _mark_npc_section(target, "spell_slots", staged_visibility)
        else:
            if op == "spend":
                new_resources, _delta = _spend(target.resources, name=resource, amount=int(args.get("amount", 1) or 1), mutation_id=mutation_id)
            elif op == "restore":
                new_resources, _delta = _restore(target.resources, name=resource, amount=int(args.get("amount", 1) or 1), mutation_id=mutation_id)
            elif op == "set":
                new_resources, _delta = _set(
                    target.resources, name=resource,
                    current=args.get("current"), maximum=args.get("maximum"), mutation_id=mutation_id,
                )
            else:
                raise ValueError(f"Staged effect {effect.get('id')!r} unknown resource op {op!r}")
            target.resources = new_resources
            _mark_npc_section(target, "resources", staged_visibility)
    except _StateError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid resource transition ({exc.code}): {exc}") from exc
    target.commit(db)
    logger.info(
        "effect apply_resource effect_id=%s target=%s:%s op=%s resource=%s slot=%s",
        effect.get("id"), args.get("target_kind"), args.get("target_id"), op, resource, slot_level,
    )


@register("apply_concentration")
def _handle_apply_concentration(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a concentration start/replace/break to canonical state (issue #227)."""
    from app.rules.state import StateError as _StateError
    from app.rules.state import break_concentration as _break
    from app.rules.state import replace_concentration as _replace
    from app.rules.state import start_concentration as _start

    args = effect.get("arguments") or {}
    op = args.get("op")
    mutation_id = args.get("mutation_id")
    target = _load_state_target(db, campaign, effect)
    staged_visibility = args.get("visibility") or "dm_private"
    try:
        if op == "start":
            new_state, _started = _start(
                target.concentration,
                effect_name=args.get("effect_name"),
                effect_id=args.get("concentration_effect_id"),
                source=args.get("source"),
                visibility=staged_visibility,
                provenance=args.get("provenance"),
                mutation_id=mutation_id,
            )
        elif op == "replace":
            new_state, _started, _broke = _replace(
                target.concentration,
                effect_name=args.get("effect_name"),
                effect_id=args.get("concentration_effect_id"),
                source=args.get("source"),
                visibility=staged_visibility,
                provenance=args.get("provenance"),
                mutation_id=mutation_id,
            )
        elif op == "break":
            new_state, _broke = _break(target.concentration, reason=args.get("reason"), mutation_id=mutation_id)
        else:
            raise ValueError(f"Staged effect {effect.get('id')!r} unknown concentration op {op!r}")
    except _StateError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid concentration transition ({exc.code}): {exc}") from exc
    target.concentration = new_state
    _mark_npc_section(target, "concentration", staged_visibility)
    target.commit(db)
    logger.info(
        "effect apply_concentration effect_id=%s target=%s:%s op=%s",
        effect.get("id"), args.get("target_kind"), args.get("target_id"), op,
    )


@register("apply_death_save")
def _handle_apply_death_save(db: Session, campaign: Campaign, effect: dict[str, Any], turn: DmTurn, attempt: DmTurnAttempt):
    """Apply a death-save record/reset with baseline unconscious/death hooks (issue #227).

    Hooks (all code-owned, inside the same atomic commit):
    - first save at 0 HP, or stabilization, ensures the ``unconscious``
      condition is structurally present;
    - death breaks active concentration;
    - natural-20 revival restores 1 HP and clears ``unconscious``.
    """
    from app.rules.state import StateError as _StateError
    from app.rules.state import add_condition as _add
    from app.rules.state import break_concentration as _break_conc
    from app.rules.state import has_condition as _has
    from app.rules.state import record_death_save as _record
    from app.rules.state import remove_condition as _remove
    from app.rules.state import reset_death_saves as _reset

    args = effect.get("arguments") or {}
    op = args.get("op")
    mutation_id = args.get("mutation_id")
    target = _load_state_target(db, campaign, effect)
    staged_visibility = args.get("visibility") or "dm_private"
    try:
        if op == "record":
            pre_s, pre_f = target.successes, target.failures
            state = _record(target.successes, target.failures, result=args.get("result"), mutation_id=mutation_id)
            target.successes, target.failures = state.successes, state.failures
            _mark_npc_section(target, "death_saves", staged_visibility)
            if (pre_s, pre_f) == (0, 0) and target.hp_current == 0 and not _has(target.conditions, "unconscious"):
                target.conditions, _rec = _add(
                    target.conditions, name="unconscious", source="death_saves",
                    description="Unconscious at 0 hit points; making death saving throws.",
                    visibility=staged_visibility,
                    provenance={"mutation_id": mutation_id}, mutation_id=f"{mutation_id}:unconscious",
                )
                _mark_npc_section(target, "conditions", staged_visibility)
            if state.stabilized and not _has(target.conditions, "unconscious"):
                target.conditions, _rec = _add(
                    target.conditions, name="unconscious", source="death_saves",
                    description="Stable but unconscious.",
                    visibility=staged_visibility,
                    provenance={"mutation_id": mutation_id}, mutation_id=f"{mutation_id}:stable",
                )
                _mark_npc_section(target, "conditions", staged_visibility)
            if state.dead:
                try:
                    broke_state, _broke = _break_conc(target.concentration, reason="dead", mutation_id=f"{mutation_id}:conc")
                    target.concentration = broke_state
                    _mark_npc_section(target, "concentration", staged_visibility)
                except _StateError:
                    pass
            if state.outcome == "revived":
                target.hp_current = max(target.hp_current or 0, state.revived_hp)
                if _has(target.conditions, "unconscious"):
                    target.conditions, _rem = _remove(target.conditions, name="unconscious", mutation_id=f"{mutation_id}:wake")
                    _mark_npc_section(target, "conditions", staged_visibility)
        elif op == "reset":
            state = _reset(target.successes, target.failures, reason=args.get("reset_reason"), mutation_id=mutation_id)
            target.successes, target.failures = state.successes, state.failures
            _mark_npc_section(target, "death_saves", staged_visibility)
        else:
            raise ValueError(f"Staged effect {effect.get('id')!r} unknown death-save op {op!r}")
    except _StateError as exc:
        raise ValueError(f"Staged effect {effect.get('id')!r} invalid death-save transition ({exc.code}): {exc}") from exc
    target.commit(db)
    logger.info(
        "effect apply_death_save effect_id=%s target=%s:%s op=%s result=%s outcome=%s",
        effect.get("id"), args.get("target_kind"), args.get("target_id"), op,
        args.get("result"), target.successes if op == "reset" else None,
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
