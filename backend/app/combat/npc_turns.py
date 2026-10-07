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

One cue per turn: a DM turn that leaves the NPC's turn open does not re-cue.
The active-encounter context record still shows the open turn, so the DM can
finish it on the next table turn.
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


def _cue_text(encounter: Encounter, participant: EncounterParticipant) -> str:
    return (
        f"[Combat] It is {participant.display_name}'s turn (encounter {encounter.id}, "
        f"participant {participant.id}, round {int(encounter.round or 1)}, "
        f"turn {int(encounter.turn_sequence or 0)}). You run this NPC: decide what it does "
        "this turn, then end its turn with npc_end_turn."
    )


def queue_npc_turn(db: Session, campaign: Campaign, encounter: Encounter) -> Any | None:
    """Queue the AI DM's cue when the active turn belongs to an NPC; once per turn.

    Runs in the transaction that starts the turn, so a rolled-back turn
    start leaves no cue. Returns the cue submission, or None.
    """
    from app.submissions.service import accept_submission
    from models.threads import PlayerSubmission

    if encounter.status != "active" or encounter.active_participant_id is None:
        return None
    participant = db.get(EncounterParticipant, encounter.active_participant_id)
    if participant is None or participant.kind not in NPC_KINDS:
        return None
    audience = _thread_audience(db, encounter.thread_id)
    if audience is None:
        return None
    text = _cue_text(encounter, participant)
    existing = db.execute(
        select(PlayerSubmission).where(
            PlayerSubmission.campaign_id == campaign.id,
            PlayerSubmission.thread_id == str(encounter.thread_id),
            PlayerSubmission.source == NPC_TURN_SOURCE,
            PlayerSubmission.raw_content == text,
        ).limit(1)
    ).scalars().first()
    if existing is not None:
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
        turn_sequence=int(encounter.turn_sequence or 0), submission_id=str(cue.id),
    )
    return cue


def coordinate_npc_turn(db: Session, campaign_id: Any, thread_id: str) -> str | None:
    """Assemble a DM turn for a waiting NPC cue; returns its prepared attempt id.

    Call after the transaction that queued the cue commits. A thread whose
    DM turn is mid-stream keeps the cue for the sweep (or the post-commit
    chain in ``execute_committed_attempt``).
    """
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
    except (StreamBoundaryError, TurnConflictError) as exc:
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
