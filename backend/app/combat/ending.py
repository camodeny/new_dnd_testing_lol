"""DM-controlled encounter end + post-combat consequence hooks — issue #239.

Code-owned authority (never delegated to models):

- The AI DM (the only DM) ends structured initiative through the
  ``end_encounter`` staged effect with an explicit outcome/reason pair. Supported
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
  in the end transaction; a hook never reopens or invalidates the ended
  encounter.
- Ending is idempotent on (encounter_id, end_operation_id): a replayed end
  effect is a no-op without duplicating rewards/events/state transitions.
- Manual post-combat interaction (searching bodies/rooms) stays available
  via normal chat: the submission path never gates on encounter status.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.replacements import PcLifecycleError, declare_pc_death
from app.characters.service import latest_sheet
from app.clock import ms_between, utcnow
from app.combat.service import (
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


def _read_final_hp(db: Session, participant: EncounterParticipant) -> dict | None:
    """Best-effort final HP snapshot from the canonical store (read-only).

    Never fails the end: a missing/malformed sheet records None rather than
    blocking the lifecycle transition.
    """
    try:
        if participant.kind == "pc" and participant.character_id is not None:

            sheet = latest_sheet(db, participant.character_id)
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

            sheet = latest_sheet(db, participant.character_id)
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
    ended_at = utcnow()
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
    encounter.end_duration_ms = ms_between(encounter.initiated_at, ended_at)
    encounter.blocked_since = None
    db.flush()
    hooks = _seed_followups(db, encounter)
    db.flush()
    return declared_deaths, hooks


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
