"""Deterministic turn progression + action economy + skip votes — issue #231.

Code-owned authority (never delegated to models):
- Per-participant turn resources (action / bonus action / movement /
  reaction / extensible per-turn extras) initialized at initiative-ready and
  reset at each participant's own turn start.
- Active-turn ownership: turn-bound consumption requires the active
  participant, executed by its controller (PC) or the campaign owner
  (NPC/monster runtime path). Reactions are the explicit rule-permitted
  exception: any participant may consume its own reaction off-turn.
- Explicit human end-turn bound to the observed turn sequence (source turn):
  stale sequences fail closed as conflicts, never double-advance.
- Missing human PCs block progression: nobody else may end their turn; only
  a party/owner skip vote advances past them, generating no actions.
- IC/OOC chat is never gated on mechanical turns (no encounter check lives
  on the submission path by design; covered by test).

Turn transitions emit ``encounter.turn_ended`` / ``encounter.turn_skipped``
plus ``encounter.turn_started`` domain events atomically with the state
change (two campaign-revision bumps in one transaction, flush-only when the
outer idempotent command owns the commit).
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.combat.service import (
    TURN_ENDED_EVENT,
    TURN_SKIPPED_EVENT,
    TURN_STARTED_EVENT,
    EncounterError,
    get_turn_order,
    list_participants,
)
from app.observability.tracing import structured_log
from models.campaigns import Campaign, CampaignMember
from models.characters import Character
from models.combat import Encounter, EncounterParticipant, EncounterSkipVote, EncounterTurnState

logger = logging.getLogger(__name__)

CONSUMABLE_RESOURCES = ("action", "bonus_action", "movement", "reaction")

# Majority of eligible voters (campaign members excluding the target's
# controller), minimum 1. The owner holds no extra weight: one member, one
# vote. The target's controller never votes — they end their own turn.
def skip_threshold(eligible_voter_count: int) -> int:
    return max(1, eligible_voter_count // 2 + 1)


class TurnError(EncounterError):
    """Deterministic turn validation failure — caller must block, never guess."""


class TurnAuthorizationError(PermissionError):
    pass


class StaleTurnError(TurnError):
    """The bound source turn already advanced; retry against current state."""

    def __init__(self, expected: int, actual: int):
        self.expected_sequence = expected
        self.actual_sequence = actual
        super().__init__(
            f"turn already advanced (expected turn_sequence {expected}, current {actual}); "
            "refresh turn state and retry"
        )


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


def _lock_encounter(db: Session, encounter_id: uuid.UUID) -> Encounter:
    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise TurnError(f"Encounter {encounter_id} not found")
    return encounter


def _require_playable(db: Session, campaign_id: uuid.UUID) -> Campaign:
    from app.campaigns.service import require_playable_campaign

    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise TurnError(f"Campaign {campaign_id} not found")
    require_playable_campaign(campaign)
    return campaign


def _active_or_raise(db: Session, encounter: Encounter) -> EncounterParticipant:
    if encounter.status != "active" or encounter.active_participant_id is None:
        raise TurnError("no active turn: encounter initiative is not complete")
    participant = db.get(EncounterParticipant, encounter.active_participant_id)
    if participant is None:
        raise TurnError("active participant no longer exists")
    return participant


def _is_owner(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    campaign = db.get(Campaign, campaign_id)
    return campaign is not None and str(campaign.owner_id) == str(user_id)


def _is_member(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return False
    if str(campaign.owner_id) == str(user_id):
        return True
    return (
        db.execute(
            select(CampaignMember).where(
                CampaignMember.campaign_id == campaign_id,
                CampaignMember.user_id == user_id,
            )
        ).scalars().first()
        is not None
    )


def _check_actor_for(db: Session, encounter: Encounter, participant: EncounterParticipant, actor_id: uuid.UUID) -> None:
    """Only the controlling player may execute a PC's turn-bound actions."""
    if participant.kind == "pc":
        if participant.controller_user_id is None or str(participant.controller_user_id) != str(actor_id):
            raise TurnAuthorizationError("Only the PC's controller may act on its turn")
        character = db.get(Character, participant.character_id) if participant.character_id else None
        if character is None or str(character.owner_id) != str(actor_id):
            raise TurnAuthorizationError("Character control changed; turn action refused")
    else:
        if not _is_owner(db, encounter.campaign_id, actor_id):
            raise TurnAuthorizationError("Only the campaign owner may act for NPC/monster turns")


def _record_invalid_attempt(db: Session, encounter: Encounter, *, reason: str, commit: bool) -> None:
    encounter.invalid_attempt_count = int(encounter.invalid_attempt_count or 0) + 1
    db.flush()
    # Persist the counter even though the attempt itself fails: commit the
    # increment first when this call owns the transaction. Inside an outer
    # idempotent command (commit=False) the flush rolls back with the failed
    # command; the structured log below remains the durable signal there.
    if commit:
        db.commit()
    structured_log(
        logger, logging.WARNING, "encounter_turn_invalid_attempt",
        encounter_id=str(encounter.id), campaign_id=str(encounter.campaign_id),
        reason=reason, invalid_attempt_count=int(encounter.invalid_attempt_count or 0),
    )


def _resolve_movement_max(db: Session, participant: EncounterParticipant) -> int:
    """Deterministic speed resolution with a fixed fallback (never blocks ready)."""
    if participant.kind == "pc" and participant.character_id is not None:
        try:
            from app.rules.mechanics import get_character_mechanics

            mechanics = get_character_mechanics(db, participant.character_id)
            return max(0, min(500, int(mechanics.combat["speed"]["value"])))
        except Exception:
            logger.warning(
                "turn_state pc speed fallback participant_id=%s", participant.id,
            )
            return 30
    try:
        from models.world import WorldEntity

        entity = db.get(WorldEntity, participant.npc_entity_id) if participant.npc_entity_id else None
        details = entity.details if entity is not None and isinstance(entity.details, dict) else {}
        for key in ("speed", "movement", "movement_speed"):
            raw = details.get(key)
            if raw is None:
                continue
            match = re.search(r"-?\d+", str(raw))
            if match:
                return max(0, min(500, int(match.group(0))))
    except Exception:
        logger.warning("turn_state npc speed fallback participant_id=%s", participant.id)
    return 30


def _reset_state_for_new_turn(state: EncounterTurnState, *, now: datetime) -> None:
    state.action_available = True
    state.bonus_action_available = True
    state.reaction_available = True
    state.movement_remaining = int(state.movement_max)
    extras = dict(state.extra_resources or {})
    for name, entry in extras.items():
        if isinstance(entry, dict) and "max" in entry:
            try:
                extras[name] = {"max": int(entry["max"]), "remaining": int(entry["max"])}
            except (TypeError, ValueError):
                continue
    state.extra_resources = extras
    state.turn_started_at = now
    state.turn_ended_at = None


def get_turn_state_row(db: Session, encounter_id: uuid.UUID, participant_id: uuid.UUID) -> EncounterTurnState | None:
    return db.execute(
        select(EncounterTurnState).where(
            EncounterTurnState.encounter_id == encounter_id,
            EncounterTurnState.participant_id == participant_id,
        )
    ).scalars().first()


def list_turn_states(db: Session, encounter_id: uuid.UUID) -> list[EncounterTurnState]:
    return list(db.execute(
        select(EncounterTurnState).where(EncounterTurnState.encounter_id == encounter_id)
    ).scalars().all())


def init_turn_states(db: Session, encounter: Encounter, *, now: datetime | None = None) -> list[EncounterTurnState]:
    """Create full-budget turn resources for every participant (ready time)."""
    now = now or _now()
    rows: list[EncounterTurnState] = []
    for participant in list_participants(db, encounter.id):
        existing = get_turn_state_row(db, encounter.id, participant.id)
        if existing is not None:
            rows.append(existing)
            continue
        movement_max = _resolve_movement_max(db, participant)
        row = EncounterTurnState(
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            participant_id=participant.id,
            action_available=True,
            bonus_action_available=True,
            reaction_available=True,
            movement_remaining=movement_max,
            movement_max=movement_max,
            extra_resources={},
            turn_started_at=now if participant.id == encounter.active_participant_id else None,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def grant_extra_resource(
    db: Session,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    name: str,
    maximum: int,
    commit: bool = True,
) -> EncounterTurnState:
    """Seed an extensible per-turn resource (e.g. future mechanics wiring)."""
    name = (name or "").strip()
    if not name or len(name) > 64 or not re.fullmatch(r"[A-Za-z0-9_:-]+", name):
        raise TurnError("extra resource name must match [A-Za-z0-9_:-]+ (1-64 chars)")
    if not isinstance(maximum, int) or not 1 <= maximum <= 99:
        raise TurnError("extra resource maximum must be an integer between 1 and 99")
    encounter = _lock_encounter(db, encounter_id)
    if encounter.status != "active":
        raise TurnError("extra resources require an active encounter")
    state = get_turn_state_row(db, encounter.id, participant_id)
    if state is None:
        raise TurnError("participant has no turn state in this encounter")
    extras = dict(state.extra_resources or {})
    extras[name] = {"max": maximum, "remaining": maximum}
    state.extra_resources = extras
    db.flush()
    if commit:
        db.commit()
        db.refresh(state)
    return state


def consume_resource(
    db: Session,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    resource: str,
    amount: int = 1,
    expected_turn_sequence: int,
    commit: bool = True,
) -> EncounterTurnState:
    """Consume one turn-bound resource under the active-turn gate.

    Action / bonus action / movement require the participant to hold the
    active turn. Reaction is the explicit rule-permitted exception: any
    participant may consume its own reaction off-turn. ``extra:<name>``
    consumes a seeded per-turn resource for the active participant.
    Out-of-turn and wrong-actor attempts fail closed and count toward the
    encounter's invalid-attempt observability.

    The caller binds the source turn it observed
    (``expected_turn_sequence``): a mismatch raises StaleTurnError without
    consuming, so a delayed command from turn N can never spend the same
    participant's budget when they become active again in a later round.
    """
    encounter = _lock_encounter(db, encounter_id)
    _require_playable(db, encounter.campaign_id)
    try:
        expected_turn_sequence = int(expected_turn_sequence)
    except (TypeError, ValueError) as exc:
        raise TurnError("expected_turn_sequence must be an integer") from exc
    if expected_turn_sequence != int(encounter.turn_sequence or 0):
        raise StaleTurnError(expected_turn_sequence, int(encounter.turn_sequence or 0))
    participant = db.get(EncounterParticipant, participant_id)
    if participant is None or participant.encounter_id != encounter.id:
        raise TurnError("participant not found in this encounter")
    active = _active_or_raise(db, encounter)
    state = get_turn_state_row(db, encounter.id, participant.id)
    if state is None:
        raise TurnError("participant has no turn state in this encounter")

    resource = (resource or "").strip()
    extra_name: str | None = None
    if resource.startswith("extra:"):
        extra_name = resource[len("extra:"):]
        base = "extra"
    else:
        base = resource
    if base not in CONSUMABLE_RESOURCES and base != "extra":
        raise TurnError(f"resource must be one of {sorted(CONSUMABLE_RESOURCES)} or extra:<name>")

    try:
        _check_actor_for(db, encounter, participant, actor_id)
    except TurnAuthorizationError:
        _record_invalid_attempt(db, encounter, reason="wrong_actor", commit=commit)
        raise

    is_active = participant.id == active.id
    # Movement amounts are feet; everything else consumes a single unit.
    if base == "movement":
        try:
            amount = int(amount)
        except (TypeError, ValueError) as exc:
            raise TurnError("movement amount must be an integer number of feet") from exc
        if amount <= 0 or amount > 500:
            raise TurnError("movement amount must be between 1 and 500 feet")
    else:
        amount = 1

    if base == "reaction":
        # Explicit exception: reactions fire off-turn by design (opportunity
        # windows themselves remain a separate issue).
        if not state.reaction_available:
            raise TurnError("reaction already consumed this round")
        state.reaction_available = False
    else:
        if not is_active:
            _record_invalid_attempt(db, encounter, reason="out_of_turn", commit=commit)
            raise TurnError("only the active participant may consume turn-bound resources")
        if base == "action":
            if not state.action_available:
                raise TurnError("action already consumed this turn")
            state.action_available = False
        elif base == "bonus_action":
            if not state.bonus_action_available:
                raise TurnError("bonus action already consumed this turn")
            state.bonus_action_available = False
        elif base == "movement":
            if int(state.movement_remaining) < amount:
                raise TurnError(
                    f"insufficient movement: {state.movement_remaining} ft remaining, {amount} ft requested"
                )
            state.movement_remaining = int(state.movement_remaining) - amount
        elif base == "extra":
            extras = dict(state.extra_resources or {})
            entry = extras.get(extra_name or "")
            if not isinstance(entry, dict) or int(entry.get("remaining", 0)) < 1:
                raise TurnError(f"extra resource {extra_name!r} is not available this turn")
            extras[extra_name or ""] = {"max": int(entry.get("max", 0)), "remaining": int(entry.get("remaining", 0)) - 1}
            state.extra_resources = extras
    db.flush()
    structured_log(
        logger, logging.INFO, "encounter_turn_consumed",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        resource=resource, amount=amount, turn_sequence=int(encounter.turn_sequence or 0),
    )
    if commit:
        db.commit()
        db.refresh(state)
    return state


# ── Skip votes ──────────────────────────────────────────────────────────────


def _eligible_voters(db: Session, campaign_id: uuid.UUID, target: EncounterParticipant) -> list[uuid.UUID]:
    members = db.execute(
        select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
    ).scalars().all()
    campaign = db.get(Campaign, campaign_id)
    ids: list[uuid.UUID] = []
    if campaign is not None and str(campaign.owner_id) != str(target.controller_user_id):
        ids.append(campaign.owner_id)
    for member in members:
        if str(member.user_id) == str(target.controller_user_id):
            continue
        if any(str(existing) == str(member.user_id) for existing in ids):
            continue
        ids.append(member.user_id)
    return ids


def skip_tally(db: Session, encounter: Encounter, target_id: uuid.UUID) -> dict:
    target = db.get(EncounterParticipant, target_id)
    if target is None or target.encounter_id != encounter.id:
        raise TurnError("skip target not found in this encounter")
    eligible = _eligible_voters(db, encounter.campaign_id, target)
    votes = db.execute(
        select(EncounterSkipVote).where(
            EncounterSkipVote.encounter_id == encounter.id,
            EncounterSkipVote.target_participant_id == target.id,
            EncounterSkipVote.turn_sequence == int(encounter.turn_sequence or 0),
        )
    ).scalars().all()
    voters = sorted({str(v.voter_user_id) for v in votes if v.voter_user_id is not None})
    threshold = skip_threshold(len(eligible))
    return {
        "target_participant_id": str(target.id),
        "turn_sequence": int(encounter.turn_sequence or 0),
        "votes": voters,
        "vote_count": len(voters),
        "eligible_voter_count": len(eligible),
        "threshold": threshold,
        "reached": len(voters) >= threshold,
    }


def cast_skip_vote(
    db: Session,
    encounter_id: uuid.UUID,
    target_participant_id: uuid.UUID,
    *,
    voter_id: uuid.UUID,
    expected_revision: int,
    expected_turn_sequence: int,
    operation_id: str | None = None,
    commit: bool = True,
) -> tuple[dict, bool, Encounter, Any | None, Any | None]:
    """Cast one skip vote; execute the skip once the party threshold is met.

    Returns (tally, executed, encounter, skipped_event, started_event).
    A repeated vote by the same voter replays the current tally without
    double-counting. Execution advances initiative with ``skipped=True`` and
    generates no actions for the absent PC — they are never AI-played.

    The voter binds the source turn observed (``expected_turn_sequence``):
    a mismatch raises StaleTurnError without recording, so a delayed vote
    from turn N can never count against the same PC when they block again
    in a later round.
    """
    started = _now()
    encounter = _lock_encounter(db, encounter_id)
    campaign = _require_playable(db, encounter.campaign_id)
    try:
        expected_turn_sequence = int(expected_turn_sequence)
    except (TypeError, ValueError) as exc:
        raise TurnError("expected_turn_sequence must be an integer") from exc
    if expected_turn_sequence != int(encounter.turn_sequence or 0):
        raise StaleTurnError(expected_turn_sequence, int(encounter.turn_sequence or 0))
    if encounter.status != "active":
        raise TurnError("skip votes require an active encounter")
    if not _is_member(db, encounter.campaign_id, voter_id):
        raise TurnAuthorizationError("Only campaign members may vote to skip a turn")
    target = db.get(EncounterParticipant, target_participant_id)
    if target is None or target.encounter_id != encounter.id:
        raise TurnError("skip target not found in this encounter")
    if target.kind != "pc" or target.controller_user_id is None:
        raise TurnError("skip votes target an absent human PC; NPC/monster turns end through the owner")
    if str(target.controller_user_id) == str(voter_id):
        raise TurnError("the controller ends their own turn instead of voting to skip it")
    active = _active_or_raise(db, encounter)
    if target.id != active.id:
        raise TurnError("skip votes target the currently active (blocking) participant")

    existing = db.execute(
        select(EncounterSkipVote).where(
            EncounterSkipVote.encounter_id == encounter.id,
            EncounterSkipVote.target_participant_id == target.id,
            EncounterSkipVote.turn_sequence == int(encounter.turn_sequence or 0),
            EncounterSkipVote.voter_user_id == voter_id,
        )
    ).scalars().first()
    if existing is None:
        db.add(EncounterSkipVote(
            encounter_id=encounter.id,
            campaign_id=encounter.campaign_id,
            target_participant_id=target.id,
            voter_user_id=voter_id,
            turn_sequence=int(encounter.turn_sequence or 0),
        ))
        db.flush()
        if encounter.blocked_since is None:
            encounter.blocked_since = _now()
            db.flush()
    tally = skip_tally(db, encounter, target.id)

    executed = False
    skipped_event: Any | None = None
    started_event: Any | None = None
    if tally["reached"]:
        encounter, skipped_event, started_event = _advance(
            db, campaign, encounter,
            actor_id=voter_id, skipped=True,
            expected_revision=expected_revision,
            operation_id=operation_id or f"encounter:{encounter.id}:turn:{encounter.turn_sequence}:skip",
            commit=False,
        )
        executed = True
    elif commit:
        # Vote recorded without executing: still needs a commit when this
        # call owns the transaction (direct service use).
        pass
    latency_ms = _ms_between(started, _now())
    structured_log(
        logger, logging.INFO, "encounter_skip_vote",
        encounter_id=str(encounter.id), target_participant_id=str(target.id),
        voter_id=str(voter_id), vote_count=tally["vote_count"],
        threshold=tally["threshold"], executed=executed,
        latency_ms=latency_ms,
    )
    if commit:
        db.commit()
        db.refresh(encounter)
    return tally, executed, encounter, skipped_event, started_event


# ── End turn + core advance ─────────────────────────────────────────────────


def end_turn(
    db: Session,
    encounter_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    expected_turn_sequence: int,
    expected_revision: int,
    operation_id: str | None = None,
    commit: bool = True,
) -> tuple[Encounter, Any, Any]:
    """Explicitly end the active turn and advance initiative deterministically.

    The caller binds the source turn it observed (``expected_turn_sequence``):
    a mismatch raises StaleTurnError without advancing, so duplicate or
    replayed commands can never advance initiative twice. Transactional: the
    state change and both domain events commit atomically.
    Returns (encounter, ended_event, started_event).
    """
    started = _now()
    encounter = _lock_encounter(db, encounter_id)
    campaign = _require_playable(db, encounter.campaign_id)
    if encounter.status != "active":
        raise TurnError("no active turn to end: encounter initiative is not complete")
    try:
        expected_turn_sequence = int(expected_turn_sequence)
    except (TypeError, ValueError) as exc:
        raise TurnError("expected_turn_sequence must be an integer") from exc
    if expected_turn_sequence != int(encounter.turn_sequence or 0):
        raise StaleTurnError(expected_turn_sequence, int(encounter.turn_sequence or 0))
    active = _active_or_raise(db, encounter)
    try:
        _check_actor_for(db, encounter, active, actor_id)
    except TurnAuthorizationError:
        # A missing human's turn blocks progression: nobody else may end it.
        # The party path is an explicit skip vote, never a foreign end-turn.
        _record_invalid_attempt(db, encounter, reason="foreign_end_turn", commit=commit)
        raise
    encounter, ended_event, started_event = _advance(
        db, campaign, encounter,
        actor_id=actor_id, skipped=False,
        expected_revision=expected_revision,
        operation_id=operation_id or f"encounter:{encounter.id}:turn:{encounter.turn_sequence}:end",
        end_turn_latency_ms=_ms_between(started, _now()),
        commit=False,
    )
    duration_ms = int(encounter.last_turn_duration_ms or 0)
    structured_log(
        logger, logging.INFO, "encounter_turn_ended",
        encounter_id=str(encounter.id), actor_id=str(actor_id),
        turn_sequence=int(encounter.turn_sequence or 0),
        turn_duration_ms=duration_ms,
        end_turn_latency_ms=int(encounter.last_end_turn_latency_ms or 0),
        round=int(encounter.round or 1),
    )
    if commit:
        db.commit()
        db.refresh(encounter)
        db.refresh(ended_event)
        db.refresh(started_event)
    return encounter, ended_event, started_event


def _advance(
    db: Session,
    campaign: Campaign,
    encounter: Encounter,
    *,
    actor_id: uuid.UUID,
    skipped: bool,
    expected_revision: int,
    operation_id: str,
    end_turn_latency_ms: int = 0,
    commit: bool = False,
) -> tuple[Encounter, Any, Any]:
    """Advance to the next turn in initiative order (round rollover included)."""
    from app.campaigns.events import commit_campaign_mutation

    order = get_turn_order(db, encounter.id)
    if not order:
        raise TurnError("cannot advance: turn order is empty")
    try:
        position = next(i for i, p in enumerate(order) if p.id == encounter.active_participant_id)
    except StopIteration as exc:
        raise TurnError("active participant is not in the authoritative turn order") from exc

    now = _now()
    ended_participant = order[position]
    ended_state = get_turn_state_row(db, encounter.id, ended_participant.id)
    turn_duration_ms = _ms_between(encounter.turn_started_at, now)
    if ended_state is not None:
        ended_state.turn_ended_at = now
        # A skipped PC generates no actions: their remaining budget is left
        # untouched (never consumed, never AI-played); it resets fresh when
        # their next turn starts.
    next_position = position + 1
    next_round = int(encounter.round or 1)
    rolled_over = False
    if next_position >= len(order):
        next_position = 0
        next_round += 1
        rolled_over = True
    next_participant = order[next_position]
    next_state = get_turn_state_row(db, encounter.id, next_participant.id)
    if next_state is None:
        raise TurnError("next participant has no turn state; turn state is corrupt")
    _reset_state_for_new_turn(next_state, now=now)

    source_sequence = int(encounter.turn_sequence or 0)
    encounter.active_participant_id = next_participant.id
    encounter.active_index = next_position
    encounter.round = next_round
    encounter.turn_sequence = source_sequence + 1
    encounter.turn_started_at = now
    encounter.blocked_since = None
    encounter.revision = int(encounter.revision or 1) + 1
    encounter.last_turn_duration_ms = turn_duration_ms
    encounter.last_end_turn_latency_ms = int(end_turn_latency_ms)
    if skipped:
        encounter.skipped_count = int(encounter.skipped_count or 0) + 1
    db.flush()

    end_kind = TURN_SKIPPED_EVENT if skipped else TURN_ENDED_EVENT
    end_operation_id = (operation_id or "").strip()[:128] or (
        f"encounter:{encounter.id}:turn:{source_sequence}:{'skip' if skipped else 'end'}"
    )
    start_operation_id = f"encounter:{encounter.id}:turn:{source_sequence + 1}:started"
    ended_payload = {
        "encounter_id": str(encounter.id),
        "thread_id": encounter.thread_id,
        "source_turn_sequence": source_sequence,
        "next_turn_sequence": source_sequence + 1,
        "ended_participant_id": str(ended_participant.id),
        "next_participant_id": str(next_participant.id),
        "round": next_round,
        "rolled_over": rolled_over,
        "skipped": skipped,
        "turn_duration_ms": turn_duration_ms,
        "end_turn_latency_ms": int(end_turn_latency_ms),
    }
    _, end_event = commit_campaign_mutation(
        db, campaign.id, expected_revision=int(expected_revision),
        event_type=end_kind, payload=ended_payload,
        operation_id=end_operation_id, actor_id=actor_id,
        outbox_event_type=end_kind,
        outbox_payload={
            "encounter_id": str(encounter.id), "campaign_id": str(encounter.campaign_id),
            "thread_id": encounter.thread_id,
            "source_turn_sequence": source_sequence,
            "next_turn_sequence": source_sequence + 1,
            "next_participant_id": str(next_participant.id),
            "round": next_round, "skipped": skipped,
        },
        outbox_operation_id=end_operation_id,
        commit=False,
    )
    _, start_event = commit_campaign_mutation(
        db, campaign.id, expected_revision=int(expected_revision) + 1,
        event_type=TURN_STARTED_EVENT,
        payload={
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "turn_sequence": source_sequence + 1,
            "active_participant_id": str(next_participant.id),
            "round": next_round,
            "rolled_over": rolled_over,
        },
        operation_id=start_operation_id, actor_id=actor_id,
        outbox_event_type=TURN_STARTED_EVENT,
        outbox_payload={
            "encounter_id": str(encounter.id), "campaign_id": str(encounter.campaign_id),
            "thread_id": encounter.thread_id,
            "turn_sequence": source_sequence + 1,
            "active_participant_id": str(next_participant.id),
            "round": next_round,
        },
        outbox_operation_id=start_operation_id,
        commit=False,
    )
    if commit:
        db.commit()
        db.refresh(encounter)
    return encounter, end_event, start_event


# ── Read projection (reconnect-safe) ────────────────────────────────────────


def turn_projection(db: Session, encounter: Encounter) -> dict | None:
    """Full turn/round/resource projection; None while initiative is pending."""
    if encounter.status != "active":
        return None
    states = {str(s.participant_id): s.to_dict() for s in list_turn_states(db, encounter.id)}
    active_id = str(encounter.active_participant_id) if encounter.active_participant_id else None
    votes: list[dict] = []
    blocked_since = encounter.blocked_since.isoformat() if encounter.blocked_since else None
    if active_id is not None:
        active = db.get(EncounterParticipant, encounter.active_participant_id)
        if active is not None and active.kind == "pc":
            tally = skip_tally(db, encounter, active.id)
            if tally["vote_count"]:
                votes = db.execute(
                    select(EncounterSkipVote).where(
                        EncounterSkipVote.encounter_id == encounter.id,
                        EncounterSkipVote.target_participant_id == active.id,
                        EncounterSkipVote.turn_sequence == int(encounter.turn_sequence or 0),
                    )
                ).scalars().all()
                votes = [v.to_dict() for v in votes]
    return {
        "turn_sequence": int(encounter.turn_sequence or 0),
        "round": int(encounter.round or 1),
        "active_participant_id": active_id,
        "active_index": int(encounter.active_index or 0),
        "turn_started_at": encounter.turn_started_at.isoformat() if encounter.turn_started_at else None,
        "blocked": blocked_since is not None,
        "blocked_since": blocked_since,
        "skipped_count": int(encounter.skipped_count or 0),
        "invalid_attempt_count": int(encounter.invalid_attempt_count or 0),
        "last_turn_duration_ms": encounter.last_turn_duration_ms,
        "last_end_turn_latency_ms": encounter.last_end_turn_latency_ms,
        "resources": states,
        "skip_votes": votes,
    }
