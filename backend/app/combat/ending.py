"""DM-controlled encounter end + post-combat consequence hooks — issue #239.

Code-owned authority (never delegated to models):

- The DM (campaign owner on the AI-DM runtime path — the AI is the only DM)
  ends structured initiative with an explicit outcome/reason pair. Supported
  outcomes cover kill-based victory plus surrender, escape, capture, retreat,
  defeat, and negotiated truce; participant outcomes record per-combatant
  fates (standing, slain, unconscious, surrendered, captured, fled,
  retreated).
- The end transition is transactional: status ``pending_initiative``/``active``
  → ``ended`` with turn/reaction/movement state frozen in place. Failed end
  transactions leave the encounter active rather than half-closed.
- Final state is preserved, never copied away: canonical HP/conditions stay
  on sheets/entity details; positions and turn budgets stay on their durable
  rows; the ``encounter.ended`` domain event carries an authoritative final
  snapshot for history/review and post-turn consolidation.
- Post-combat hooks (loot availability, XP/progression, death aftermath,
  custody state, post-turn consolidation) are durable rows created pending
  in the end transaction. Normal campaign processing completes them (or
  records a failure) afterwards; a hook failure never reopens or invalidates
  the ended encounter.
- Ending is idempotent on (encounter_id, end_operation_id): a duplicate end
  command replays the original encounter + event + hooks without duplicating
  rewards/events/state transitions.
- Manual post-combat interaction (searching bodies/rooms) stays available
  via normal chat: the submission path never gates on encounter status.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.combat.service import (
    ENCOUNTER_ENDED_EVENT,
    EncounterError,
    list_participants,
)
from app.observability.tracing import structured_log
from models.campaigns import Campaign
from models.combat import (
    END_FOLLOWUP_HOOKS,
    Encounter,
    EncounterEndFollowup,
    EncounterParticipant,
)

logger = logging.getLogger(__name__)

#: DM-declared encounter outcomes. Kill-based victory needs no special case:
#: ``victory`` covers all-enemies-dead as well as routed/broken foes.
END_OUTCOMES = (
    "victory",
    "defeat",
    "surrender",
    "escape",
    "capture",
    "retreat",
    "negotiated_truce",
)

#: Per-participant fates recorded at end time. Defaults to ``standing``.
PARTICIPANT_OUTCOMES = (
    "standing",
    "slain",
    "unconscious",
    "surrendered",
    "captured",
    "fled",
    "retreated",
)

ENDABLE_STATUSES = ("pending_initiative", "active")

MAX_REASON_LENGTH = 2000


class EndEncounterError(EncounterError):
    """Deterministic encounter-end validation failure — caller must block."""


class EndEncounterAuthorizationError(PermissionError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _ms_between(start: datetime | None, end: datetime | None) -> int:
    start, end = _aware(start), _aware(end)
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _validate_end_args(
    outcome: Any, reason: Any, participant_outcomes: Any
) -> tuple[str, str, dict[str, str]]:
    outcome = str(outcome or "").strip().lower()
    if outcome not in END_OUTCOMES:
        raise EndEncounterError(
            f"outcome must be one of {sorted(END_OUTCOMES)}"
        )
    reason = str(reason or "").strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise EndEncounterError(
            f"reason is required (1-{MAX_REASON_LENGTH} characters)"
        )
    outcomes: dict[str, str] = {}
    if participant_outcomes is None:
        return outcome, reason, outcomes
    if not isinstance(participant_outcomes, dict):
        raise EndEncounterError("participant_outcomes must be an object keyed by participant id")
    for raw_pid, raw_fate in participant_outcomes.items():
        try:
            pid = str(uuid.UUID(str(raw_pid)))
        except ValueError as exc:
            raise EndEncounterError(f"participant_outcomes key {raw_pid!r} must be a UUID") from exc
        fate = str(raw_fate or "").strip().lower()
        if fate not in PARTICIPANT_OUTCOMES:
            raise EndEncounterError(
                f"participant outcome for {pid} must be one of {sorted(PARTICIPANT_OUTCOMES)}"
            )
        outcomes[pid] = fate
    return outcome, reason, outcomes


def _check_owner(db: Session, campaign: Campaign, actor_id: uuid.UUID) -> None:
    if str(campaign.owner_id) != str(actor_id):
        raise EndEncounterAuthorizationError(
            "Only the campaign owner (DM runtime path) may end an encounter"
        )


def _read_final_hp(db: Session, participant: EncounterParticipant) -> dict | None:
    """Best-effort final HP snapshot from the canonical store (read-only).

    Never fails the end: a missing/malformed sheet records None rather than
    blocking the lifecycle transition.
    """
    try:
        if participant.kind == "pc" and participant.character_id is not None:
            from models.characters import Dnd5eCharacterSheet

            sheet = db.execute(
                select(Dnd5eCharacterSheet)
                .where(Dnd5eCharacterSheet.character_id == participant.character_id)
                .order_by(Dnd5eCharacterSheet.updated_at.desc())
            ).scalars().first()
            if sheet is None:
                return None
            return {
                "current": int(sheet.hit_points_current),
                "maximum": int(sheet.hit_points_max),
                "temporary": int(sheet.hit_points_temp or 0),
            }
        if participant.npc_entity_id is not None:
            from models.world import WorldEntity

            entity = db.get(WorldEntity, participant.npc_entity_id)
            details = entity.details if entity is not None and isinstance(entity.details, dict) else {}
            nested = details.get("hit_points")
            if not isinstance(nested, dict):
                return None
            return {
                "current": int(nested.get("current", nested.get("current_hp"))),
                "maximum": int(nested.get("maximum", nested.get("max_hp", nested.get("max")))),
                "temporary": int(nested.get("temporary", nested.get("temp_hp", 0)) or 0),
            }
    except Exception:
        logger.warning("encounter_end hp snapshot skipped participant_id=%s", participant.id)
    return None


def _read_final_conditions(db: Session, participant: EncounterParticipant) -> list:
    """Best-effort final condition names from the canonical store (read-only)."""
    try:
        if participant.kind == "pc" and participant.character_id is not None:
            from models.characters import Dnd5eCharacterSheet

            sheet = db.execute(
                select(Dnd5eCharacterSheet)
                .where(Dnd5eCharacterSheet.character_id == participant.character_id)
                .order_by(Dnd5eCharacterSheet.updated_at.desc())
            ).scalars().first()
            raw = (sheet.conditions or []) if sheet is not None else []
            return [str(i.get("condition", i)) for i in raw if isinstance(i, (dict, str))]
        if participant.npc_entity_id is not None:
            from models.world import WorldEntity

            entity = db.get(WorldEntity, participant.npc_entity_id)
            details = entity.details if entity is not None and isinstance(entity.details, dict) else {}
            raw = details.get("conditions") or []
            return [str(i.get("condition", i)) for i in raw if isinstance(i, (dict, str))]
    except Exception:
        logger.warning("encounter_end condition snapshot skipped participant_id=%s", participant.id)
    return []


def build_final_snapshot(db: Session, encounter: Encounter) -> dict:
    """Authoritative final state for the ended-event payload/history.

    Reads (never writes): canonical HP/conditions, durable placements, and
    frozen turn budgets. Hidden NPC HP/condition detail stays DM-scoped in
    the snapshot projection — the event payload carries only presence flags
    for ``dm_private`` participants (member feeds converge via the
    privacy-filtered encounter view).
    """
    from app.combat.turns import list_turn_states
    from models.combat import EncounterPlacement

    placements = {
        str(p.participant_id): {"col": int(p.col), "row": int(p.row)}
        for p in db.execute(
            select(EncounterPlacement).where(EncounterPlacement.encounter_id == encounter.id)
        ).scalars().all()
    }
    budgets = {
        str(s.participant_id): {
            "action_available": bool(s.action_available),
            "bonus_action_available": bool(s.bonus_action_available),
            "reaction_available": bool(s.reaction_available),
            "movement_remaining": int(s.movement_remaining),
        }
        for s in list_turn_states(db, encounter.id)
    }
    finals = []
    for participant in list_participants(db, encounter.id):
        pid = str(participant.id)
        hidden = participant.kind in ("npc", "monster") and participant.stat_visibility == "dm_private"
        entry: dict[str, Any] = {
            "id": pid,
            "kind": participant.kind,
            "display_name": participant.display_name,
            "initiative_total": participant.initiative_total,
            "sort_order": participant.sort_order,
            "position": placements.get(pid),
            "turn_budget": budgets.get(pid),
        }
        if hidden:
            entry["hit_points"] = None
            entry["conditions"] = []
            entry["detail_redacted"] = True
        else:
            entry["hit_points"] = _read_final_hp(db, participant)
            entry["conditions"] = _read_final_conditions(db, participant)
        finals.append(entry)
    return {
        "round": int(encounter.round or 1),
        "turn_sequence": int(encounter.turn_sequence or 0),
        "participant_count": len(finals),
        "participants": finals,
    }


def _apply_slain_deaths(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    outcomes: dict[str, str],
    *,
    reason: str,
    actor_id: uuid.UUID,
) -> list[str]:
    """Persist PC deaths from ``slain`` outcomes into the lifecycle (#266).

    Flush-only; runs inside the end transaction so a failed end leaves both
    the encounter and lifecycle rows untouched. Already-terminal PCs converge
    (duplicate-safe) instead of failing the end.
    """
    from app.campaigns.replacements import PcLifecycleError, declare_pc_death

    declared: list[str] = []
    by_id = {str(p.id): p for p in list_participants(db, encounter.id)}
    for pid, fate in outcomes.items():
        if fate != "slain":
            continue
        participant = by_id.get(pid)
        if participant is None or participant.kind != "pc" or participant.character_id is None:
            continue
        try:
            declare_pc_death(
                db, campaign, participant.character_id,
                status="dead", cause=reason[:500], actor_id=actor_id,
            )
            declared.append(str(participant.character_id))
        except PcLifecycleError as exc:
            if getattr(exc, "status_code", None) == 409 and "already" in str(exc).lower():
                # Converged: the PC is already terminal (duplicate end path
                # or a death declared mid-combat). The end still records it.
                declared.append(str(participant.character_id))
                structured_log(
                    logger, logging.INFO, "encounter_end_death_converged",
                    encounter_id=str(encounter.id),
                    character_id=str(participant.character_id),
                )
                continue
            raise
    return declared


def _seed_followups(db: Session, encounter: Encounter) -> list[EncounterEndFollowup]:
    """Create the five pending consequence hooks (idempotent per hook)."""
    rows: list[EncounterEndFollowup] = []
    for hook in END_FOLLOWUP_HOOKS:
        existing = db.execute(
            select(EncounterEndFollowup).where(
                EncounterEndFollowup.encounter_id == encounter.id,
                EncounterEndFollowup.hook_type == hook,
            )
        ).scalars().first()
        if existing is not None:
            rows.append(existing)
            continue
        row = EncounterEndFollowup(
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            hook_type=hook,
            status="pending",
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def list_end_followups(db: Session, encounter_id: uuid.UUID) -> list[EncounterEndFollowup]:
    return list(db.execute(
        select(EncounterEndFollowup)
        .where(EncounterEndFollowup.encounter_id == encounter_id)
        .order_by(EncounterEndFollowup.hook_type.asc())
    ).scalars().all())


def find_ended_event(db: Session, encounter: Encounter):
    """Resolve the encounter-ended domain event: stored id, else op lookup."""
    from models.campaigns import CampaignDomainEvent

    if encounter.ended_event_id is not None:
        event = db.get(CampaignDomainEvent, encounter.ended_event_id)
        if event is not None:
            return event
    if encounter.end_operation_id:
        return db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == encounter.campaign_id,
                CampaignDomainEvent.operation_id == encounter.end_operation_id,
                CampaignDomainEvent.event_type == ENCOUNTER_ENDED_EVENT,
            )
        ).scalars().first()
    return None


def _transition_rows(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    *,
    outcome: str,
    reason: str,
    outcomes: dict[str, str],
    operation_id: str,
    actor_id: uuid.UUID,
) -> tuple[list[str], list[EncounterEndFollowup]]:
    """Apply the end transition + deaths + hooks (flush-only, no commit)."""
    by_id = {str(p.id): p for p in list_participants(db, encounter.id)}
    unknown = [pid for pid in outcomes if pid not in by_id]
    if unknown:
        raise EndEncounterError(
            f"participant_outcomes references unknown participants: {sorted(unknown)}"
        )
    ended_at = _now()
    declared_deaths = _apply_slain_deaths(
        db, campaign, encounter, outcomes,
        reason=reason, actor_id=actor_id,
    )
    encounter.status = "ended"
    encounter.ended_at = ended_at
    encounter.revision = int(encounter.revision or 1) + 1
    encounter.end_outcome = outcome
    encounter.end_reason = reason
    encounter.ended_by = actor_id
    encounter.end_operation_id = operation_id
    encounter.end_participant_outcomes = {
        pid: outcomes.get(pid, "standing") for pid in by_id
    }
    encounter.end_duration_ms = _ms_between(encounter.initiated_at, ended_at)
    encounter.blocked_since = None
    db.flush()
    hooks = _seed_followups(db, encounter)
    db.flush()
    return declared_deaths, hooks


def end_encounter(
    db: Session,
    encounter_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    outcome: str,
    reason: str,
    participant_outcomes: dict | None = None,
    expected_revision: int,
    operation_id: str,
    commit: bool = True,
) -> tuple[Encounter, Any, list[EncounterEndFollowup]]:
    """End an encounter as a DM-declared authoritative transition.

    Idempotent on (encounter_id, end_operation_id): a duplicate end command
    replays the original encounter + ended event + hooks, bumps the
    duplicate counter, and creates nothing new. A *different* operation
    against an ended encounter fails closed as a conflict.

    Returns (encounter, ended_event, followups).
    """
    from app.campaigns.events import commit_campaign_mutation

    operation_id = (operation_id or "").strip()
    if not operation_id or len(operation_id) > 128:
        raise EndEncounterError("operation_id is required (1-128 characters)")
    outcome, reason, outcomes = _validate_end_args(outcome, reason, participant_outcomes)

    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise EndEncounterError(f"Encounter {encounter_id} not found")
    campaign = db.execute(
        select(Campaign).where(Campaign.id == encounter.campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise EndEncounterError(f"Campaign {encounter.campaign_id} not found")
    from app.campaigns.service import require_playable_campaign

    require_playable_campaign(campaign)
    _check_owner(db, campaign, actor_id)

    if encounter.status == "ended":
        encounter.duplicate_end_count = int(encounter.duplicate_end_count or 0) + 1
        db.flush()
        if encounter.end_operation_id != operation_id:
            if commit:
                db.commit()
                db.refresh(encounter)
            structured_log(
                logger, logging.WARNING, "encounter_end_conflict",
                encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
                end_operation_id=encounter.end_operation_id,
                operation_id=operation_id,
                duplicate_end_count=int(encounter.duplicate_end_count or 0),
            )
            raise EndEncounterError(
                f"encounter is already ended (outcome={encounter.end_outcome}); "
                "a duplicate end must replay the original operation_id"
            )
        if commit:
            db.commit()
            db.refresh(encounter)
        structured_log(
            logger, logging.INFO, "encounter_end_duplicate_hit",
            encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
            operation_id=operation_id,
            duplicate_end_count=int(encounter.duplicate_end_count or 0),
        )
        return encounter, find_ended_event(db, encounter), list_end_followups(db, encounter.id)

    if encounter.status not in ENDABLE_STATUSES:
        raise EndEncounterError(
            f"encounter cannot be ended from status {encounter.status}"
        )

    prior_status = encounter.status
    holder: dict[str, Any] = {}

    def _mutate(locked: Campaign) -> None:
        declared, hooks = _transition_rows(
            db, locked, encounter, outcome=outcome, reason=reason,
            outcomes=outcomes, operation_id=operation_id, actor_id=actor_id,
        )
        holder["declared_deaths"] = declared
        holder["hooks"] = hooks
        holder["final"] = build_final_snapshot(db, encounter)

    def _payload() -> dict:
        final = holder["final"]
        return {
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "prior_status": prior_status,
            "outcome": outcome,
            "reason": reason,
            "round": int(encounter.round or 1),
            "turn_sequence": int(encounter.turn_sequence or 0),
            "duration_ms": int(encounter.end_duration_ms or 0),
            "participant_outcomes": dict(encounter.end_participant_outcomes or {}),
            "declared_deaths": list(holder.get("declared_deaths") or []),
            "followup_hooks": [h.hook_type for h in (holder.get("hooks") or [])],
            "final_state": final,
            "ended_by": str(actor_id),
        }

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign.id,
        expected_revision=int(expected_revision),
        event_type=ENCOUNTER_ENDED_EVENT,
        operation_id=operation_id,
        actor_id=actor_id,
        mutate=_mutate,
        commit=False,
        payload_builder=_payload,
        outbox_event_type=ENCOUNTER_ENDED_EVENT,
        outbox_payload={
            "encounter_id": str(encounter.id),
            "campaign_id": str(encounter.campaign_id),
            "thread_id": encounter.thread_id,
            "outcome": outcome,
        },
        outbox_operation_id=f"encounter:{encounter.id}:ended",
        provenance={"source": "dm_end_encounter"},
    )
    encounter.ended_event_id = event.id
    db.flush()
    if commit:
        db.commit()
        db.refresh(encounter)
        db.refresh(event)
        db.refresh(campaign_after)
    structured_log(
        logger, logging.INFO, "encounter_ended",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        outcome=outcome, prior_status=prior_status,
        round=int(encounter.round or 1),
        turn_sequence=int(encounter.turn_sequence or 0),
        duration_ms=int(encounter.end_duration_ms or 0),
        participant_outcomes=dict(encounter.end_participant_outcomes or {}),
        declared_deaths=list(holder.get("declared_deaths") or []),
        followup_hooks=[h.hook_type for h in (holder.get("hooks") or [])],
        operation_id=operation_id, revision=int(campaign_after.revision or 0),
    )
    if commit:
        from app.realtime.service import publish_encounter_ended

        publish_encounter_ended(db, encounter)
    return encounter, event, holder.get("hooks") or []


def end_encounter_inline(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    args: dict,
    operation_key: str,
) -> Encounter:
    """DM structured-effect path: end rows inside the caller's turn-commit txn.

    No commit here — the outer turn commit owns both, and stages the
    distinct ``encounter.ended`` lifecycle event in the same outer
    transaction (resolved on read via end_operation_id). A retried effect
    with the same key against the already-ended encounter is a no-op; a new
    key against an ended encounter fails closed.
    """
    outcome, reason, outcomes = _validate_end_args(
        (args or {}).get("outcome"),
        (args or {}).get("reason"),
        (args or {}).get("participant_outcomes"),
    )
    if encounter.status == "ended":
        if encounter.end_operation_id == operation_key:
            logger.info(
                "encounter inline_end_duplicate_hit campaign_id=%s encounter_id=%s op=%s",
                campaign.id, encounter.id, operation_key,
            )
            return encounter
        raise EndEncounterError(
            f"encounter is already ended (outcome={encounter.end_outcome})"
        )
    if encounter.status not in ENDABLE_STATUSES:
        raise EndEncounterError(
            f"encounter cannot be ended from status {encounter.status}"
        )
    _transition_rows(
        db, campaign, encounter, outcome=outcome, reason=reason,
        outcomes=outcomes, operation_id=operation_key, actor_id=campaign.owner_id,
    )
    structured_log(
        logger, logging.INFO, "encounter_ended",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        outcome=outcome, prior_status="inline", source="dm_effect",
        operation_id=operation_key,
    )
    return encounter


def process_end_followup(
    db: Session,
    encounter_id: uuid.UUID,
    hook_type: str,
    *,
    result: dict | None = None,
    fail_reason: str | None = None,
    commit: bool = True,
) -> EncounterEndFollowup:
    """Record normal campaign processing against one post-combat hook.

    Either completes the hook with a validated ``result`` object or records
    a downstream ``fail_reason``. Both paths bump ``attempts``; failures
    also bump the encounter's ``followup_failure_count`` for observability.
    A hook failure never reopens or otherwise touches the ended encounter's
    lifecycle state. Completed hooks replay their recorded result instead
    of re-applying.
    """
    hook_type = str(hook_type or "").strip()
    if hook_type not in END_FOLLOWUP_HOOKS:
        raise EndEncounterError(
            f"hook_type must be one of {sorted(END_FOLLOWUP_HOOKS)}"
        )
    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise EndEncounterError(f"Encounter {encounter_id} not found")
    if encounter.status != "ended":
        raise EndEncounterError("follow-up hooks require an ended encounter")
    row = db.execute(
        select(EncounterEndFollowup).where(
            EncounterEndFollowup.encounter_id == encounter.id,
            EncounterEndFollowup.hook_type == hook_type,
        ).with_for_update()
    ).scalars().first()
    if row is None:
        raise EndEncounterError(
            f"no {hook_type} hook for encounter {encounter_id}"
        )
    if row.status == "complete":
        structured_log(
            logger, logging.INFO, "encounter_end_followup_duplicate_hit",
            encounter_id=str(encounter.id), hook_type=hook_type,
        )
        return row
    row.attempts = int(row.attempts or 0) + 1
    if fail_reason is not None:
        detail = str(fail_reason or "").strip() or "downstream processing failed"
        row.status = "failed"
        row.error = detail[:2000]
        encounter.followup_failure_count = int(encounter.followup_failure_count or 0) + 1
        db.flush()
        if commit:
            db.commit()
            db.refresh(row)
            db.refresh(encounter)
        structured_log(
            logger, logging.WARNING, "encounter_end_followup_failed",
            encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
            hook_type=hook_type, attempts=int(row.attempts or 0),
            followup_failure_count=int(encounter.followup_failure_count or 0),
            error=detail[:500],
        )
        return row
    if result is not None and not isinstance(result, dict):
        raise EndEncounterError("follow-up result must be an object")
    row.status = "complete"
    row.result = dict(result or {})
    row.error = None
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    structured_log(
        logger, logging.INFO, "encounter_end_followup_complete",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        hook_type=hook_type, attempts=int(row.attempts or 0),
    )
    return row
