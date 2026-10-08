"""The AI DM runs NPC turns — issue #236.

When an NPC or monster's turn starts, nobody at the table acts for it: code
queues one system cue in the encounter's thread (:func:`queue_npc_turn`), and
the normal DM pipeline turns that cue into a DM turn. The DM decides what the
NPC does and stages it: ``attack`` mechanics (code rolls the NPC's dice),
``consume_turn_resource``, and ``npc_end_turn`` when it is done. Code checks
every one of those against the action economy and the active turn.

The cue is a ``PlayerSubmission`` with :data:`NPC_TURN_SOURCE`, attributed to
the campaign owner like the campaign-opening cue (submissions need a user).
It never speaks as a character and is never shown to players: snapshot,
history, and realtime projections drop it.

A DM turn that answers the cue but leaves the NPC's turn open gets one
reminder cue; if that turn also leaves it open, code ends the NPC's turn
(:func:`settle_npc_turn`), so combat never stalls on a forgotten
``npc_end_turn``. While the campaign is AI-paused on capacity no cue is
queued; the DM sweep queues it once capacity returns
(:func:`queue_missing_npc_turns`).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from app.submissions.service import DM_ONLY_SUBMISSION_SOURCES
from models.campaigns import Campaign
from models.combat import Encounter, EncounterParticipant

logger = logging.getLogger(__name__)

NPC_TURN_SOURCE = DM_ONLY_SUBMISSION_SOURCES[0]
NPC_KINDS = ("npc", "monster")
#: Bound on NPC turns one post-response dispatch runs back to back.
MAX_CHAINED_NPC_TURNS = 12


def _thread_audience(db: Session, thread_id: str) -> str | None:
    """Submission audience for the encounter's thread; None when the DM never runs there."""
    from models.threads import CampaignThread

    try:
        thread = db.get(CampaignThread, uuid.UUID(str(thread_id)))
    except (ValueError, TypeError):
        thread = None
    if thread is None:
        return "campaign"
    if thread.thread_type == "lobby" or thread.private_kind == "direct":
        return None
    return "campaign" if thread.thread_type == "campaign" else "private"


def _cue_text(encounter: Encounter, participant: EncounterParticipant, *, reminder: bool = False) -> str:
    head = (
        f"[Combat] It is {participant.display_name}'s turn (encounter {encounter.id}, "
        f"participant {participant.id}, round {int(encounter.round or 1)}, "
        f"turn {int(encounter.turn_sequence or 0)})."
    )
    if reminder:
        return head + (
            " Its turn is still open: finish what it does, then end its turn with npc_end_turn. "
            "If this turn ends without npc_end_turn, code ends it."
        )
    return head + " You run this NPC: decide what it does this turn, then end its turn with npc_end_turn."


def _turn_cues(db: Session, encounter: Encounter, participant: EncounterParticipant) -> list[Any]:
    """Cues already queued for the encounter's current turn (first, then reminder)."""
    from models.threads import PlayerSubmission

    texts = [_cue_text(encounter, participant), _cue_text(encounter, participant, reminder=True)]
    return list(db.execute(
        select(PlayerSubmission).where(
            PlayerSubmission.campaign_id == encounter.campaign_id,
            PlayerSubmission.thread_id == str(encounter.thread_id),
            PlayerSubmission.source == NPC_TURN_SOURCE,
            PlayerSubmission.raw_content.in_(texts),
        ).order_by(PlayerSubmission.sequence)
    ).scalars().all())


def _active_npc(db: Session, encounter: Encounter | None) -> EncounterParticipant | None:
    if encounter is None or encounter.status != "active" or encounter.active_participant_id is None:
        return None
    participant = db.get(EncounterParticipant, encounter.active_participant_id)
    return participant if participant is not None and participant.kind in NPC_KINDS else None


def queue_npc_turn(db: Session, campaign: Campaign, encounter: Encounter, *, reminder: bool = False) -> Any | None:
    """Queue the AI DM's cue when the active turn belongs to an NPC; once per turn.

    Runs in the transaction that starts the turn, so a rolled-back turn
    start leaves no cue. Returns the cue submission, or None (not an NPC
    turn, already queued, or the campaign is AI-paused on capacity).
    """
    from app.billing.resolution_guarantee import CapacityPausedError, require_new_ai_work
    from app.submissions.service import accept_submission

    participant = _active_npc(db, encounter)
    if participant is None:
        return None
    audience = _thread_audience(db, encounter.thread_id)
    if audience is None:
        return None
    text = _cue_text(encounter, participant, reminder=reminder)
    if any(cue.raw_content == text for cue in _turn_cues(db, encounter, participant)):
        return None
    try:
        # An NPC turn is new AI work: it waits out a capacity pause like a
        # player's message (the sweep queues it when capacity returns).
        require_new_ai_work(db, campaign.id, str(encounter.thread_id))
    except CapacityPausedError:
        logger.info("npc_turn deferred encounter_id=%s reason=ai_paused_capacity", encounter.id)
        return None
    cue = accept_submission(
        db,
        campaign_id=campaign.id,
        user_id=campaign.owner_id,
        raw_content=text,
        segments=[{"type": "ooc", "text": text}],
        thread_id=str(encounter.thread_id),
        audience=audience,
        source=NPC_TURN_SOURCE,
        default_speaker=False,
    )
    structured_log(
        logger, logging.INFO, "npc_turn_queued",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        turn_sequence=int(encounter.turn_sequence or 0), submission_id=str(cue.id), reminder=reminder,
    )
    return cue


def settle_npc_turn(db: Session, campaign: Campaign, encounter: Encounter, attempt: Any) -> dict[str, Any] | None:
    """After a DM turn that answered this NPC turn's cue: remind once, then end it.

    Runs inside the DM turn commit. Returns the transition when code ended
    the turn (its events are the caller's to write), else None.
    """
    from app.combat.turns import _advance_state

    participant = _active_npc(db, encounter)
    if participant is None:
        return None
    cues = _turn_cues(db, encounter, participant)
    answered = {str(s) for s in (attempt.submission_ids or [])}
    if not any(str(cue.id) in answered for cue in cues):
        return None
    if len(cues) < 2:
        queue_npc_turn(db, campaign, encounter, reminder=True)
        return None
    transition = _advance_state(db, encounter, skipped=False)
    structured_log(
        logger, logging.WARNING, "npc_turn_auto_ended",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        turn_sequence=int(transition["source_sequence"]),
    )
    return transition


def queue_missing_npc_turns(db: Session, *, limit: int = 5) -> list[str]:
    """Sweep: queue cues an NPC turn is owed but never got (e.g. while AI-paused).

    A turn with no cue gets its first; a turn whose only cue was answered
    without ending it gets the reminder that a paused commit skipped.
    """
    rows = db.execute(
        select(Encounter, Campaign)
        .join(Campaign, Campaign.id == Encounter.campaign_id)
        .join(EncounterParticipant, EncounterParticipant.id == Encounter.active_participant_id)
        .where(
            Encounter.status == "active",
            Campaign.status != "archived",
            EncounterParticipant.kind.in_(NPC_KINDS),
        )
        .order_by(Encounter.turn_started_at.asc())
    ).all()
    queued: list[str] = []
    for encounter, campaign in rows:
        participant = _active_npc(db, encounter)
        if participant is None:
            continue
        cues = _turn_cues(db, encounter, participant)
        if len(cues) > 1 or any(cue.resolution_status == "accepted" for cue in cues):
            continue
        try:
            if queue_npc_turn(db, campaign, encounter, reminder=bool(cues)) is not None:
                db.commit()
                queued.append(str(encounter.id))
        except Exception:
            db.rollback()
            logger.warning("npc_turn sweep queue failed encounter_id=%s", encounter.id, exc_info=True)
        if len(queued) >= limit:
            break
    return queued


def coordinate_npc_turn(db: Session, campaign_id: Any, thread_id: str) -> str | None:
    """Assemble a DM turn for a waiting NPC cue; returns its prepared attempt id.

    Call after the transaction that queued the cue commits. A thread whose
    DM turn is mid-stream keeps the cue for the sweep (or the post-commit
    chain in ``execute_committed_attempt``).
    """
    from app.billing.resolution_guarantee import CapacityPausedError
    from app.dm.turns import StreamBoundaryError, TurnConflictError, coordinate_turn
    from models.dm import DmTurnAttempt
    from models.threads import PlayerSubmission

    waiting = db.execute(
        select(PlayerSubmission.id, PlayerSubmission.audience).where(
            PlayerSubmission.campaign_id == campaign_id,
            PlayerSubmission.thread_id == str(thread_id),
            PlayerSubmission.source == NPC_TURN_SOURCE,
            PlayerSubmission.resolution_status == "accepted",
        ).limit(1)
    ).first()
    if waiting is None:
        return None
    try:
        coord = coordinate_turn(db, campaign_id, str(thread_id), audience=waiting.audience, commit=True)
    except (StreamBoundaryError, TurnConflictError, CapacityPausedError) as exc:
        db.rollback()
        logger.info("npc_turn coordination deferred campaign_id=%s thread_id=%s reason=%s", campaign_id, thread_id, exc)
        return None
    except Exception:
        db.rollback()
        logger.warning("npc_turn coordination failed campaign_id=%s thread_id=%s", campaign_id, thread_id, exc_info=True)
        return None
    if coord is None:
        return None
    turn, attempt = coord
    if str(waiting.id) not in [str(s) for s in (turn.submission_ids or [])]:
        return None
    attempt = db.get(DmTurnAttempt, attempt.id)
    return str(attempt.id) if attempt is not None and attempt.status == "prepared" else None


def coordinate_encounter_npc_turn(db: Session, encounter_id: Any) -> str | None:
    """:func:`coordinate_npc_turn` for an encounter's thread (post-commit API paths)."""
    try:
        encounter = db.get(Encounter, uuid.UUID(str(encounter_id)))
    except (ValueError, TypeError):
        return None
    if encounter is None:
        return None
    return coordinate_npc_turn(db, encounter.campaign_id, encounter.thread_id)


# ── DM context ─────────────────────────────────────────────────────────────


def encounter_character_ids(db: Session, campaign_id: Any, thread_id: str) -> list[uuid.UUID]:
    """PCs fighting in the thread's active encounter (the DM's NPCs act against them)."""
    from app.combat.service import get_active_encounter, list_participants

    encounter = get_active_encounter(db, campaign_id)
    if encounter is None or str(encounter.thread_id) != str(thread_id):
        return []
    return [p.character_id for p in list_participants(db, encounter.id) if p.kind == "pc" and p.character_id]


def _npc_attacks(details: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for attack in details.get("attacks") or []:
        if isinstance(attack, dict) and attack.get("name"):
            out.append({
                key: attack.get(key)
                for key in ("name", "kind", "attack_bonus", "damage", "damage_type", "reach_ft", "range_ft")
                if attack.get(key) is not None
            })
    return out


def active_encounter_context(db: Session, campaign_id: Any, thread_id: str) -> dict[str, Any] | None:
    """DM-only view of the thread's active encounter: whose turn it is and what is left.

    ``None`` when the thread has no encounter in initiative. NPC entries
    carry their stat-block attacks and Multiattack count so the DM can pick
    an attack by name; code rolls it.
    """
    from app.combat.maps import list_placements
    from app.combat.service import get_active_encounter, get_turn_order
    from app.characters.service import latest_sheet
    from app.combat.turns import list_turn_states
    from models.world import WorldEntity

    encounter = get_active_encounter(db, campaign_id)
    if encounter is None or str(encounter.thread_id) != str(thread_id) or encounter.status != "active":
        return None
    states = {str(s.participant_id): s for s in list_turn_states(db, encounter.id)}
    cells = {str(p.participant_id): [p.col, p.row] for p in list_placements(db, encounter.id)}
    participants = []
    for p in get_turn_order(db, encounter.id):
        entry: dict[str, Any] = {
            "participant_id": str(p.id),
            "kind": p.kind,
            "name": p.display_name,
        }
        if p.kind == "pc" and p.character_id is not None:
            entry["ref"] = {"type": "character", "id": str(p.character_id)}
            sheet = latest_sheet(db, p.character_id)
            if sheet is not None:
                entry["hit_points"] = {"current": sheet.hit_points_current, "maximum": sheet.hit_points_max}
        elif p.npc_entity_id is not None:
            entry["ref"] = {"type": "npc", "id": str(p.npc_entity_id)}
            entity = db.get(WorldEntity, p.npc_entity_id)
            details = entity.details if entity is not None and isinstance(entity.details, dict) else {}
            hp = details.get("hit_points")
            if isinstance(hp, dict):
                entry["hit_points"] = {"current": hp.get("current"), "maximum": hp.get("maximum")}
            entry["attacks"] = _npc_attacks(details)
            entry["multiattack"] = int(details.get("multiattack") or 1)
        if str(p.id) in cells:
            entry["cell"] = cells[str(p.id)]
        participants.append(entry)
    active_id = str(encounter.active_participant_id)
    active = next((p for p in participants if p["participant_id"] == active_id), None)
    state = states.get(active_id)
    return {
        "encounter_id": str(encounter.id),
        "round": int(encounter.round or 1),
        "turn_sequence": int(encounter.turn_sequence or 0),
        "active_participant": active,
        "active_is_npc": bool(active and active["kind"] in NPC_KINDS),
        "active_turn_resources": {
            "action": bool(state.action_available),
            "bonus_action": bool(state.bonus_action_available),
            "reaction": bool(state.reaction_available),
            "movement_remaining_ft": int(state.movement_remaining),
        } if state is not None else None,
        "turn_order": participants,
        "grid_feet_per_square": 5,
    }
