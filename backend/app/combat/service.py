"""Authoritative encounter lifecycle — issue #230."""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.events import commit_campaign_mutation
from app.campaigns.replacements import TERMINAL_PC_STATUSES, get_lifecycle
from app.campaigns.service import lock_campaign_row, require_playable_campaign
from app.clock import ms_between, utcnow
from app.observability.tracing import structured_log
from app.realtime.service import publish_encounter_ended, publish_encounter_ready, publish_encounter_started
from app.rules.mechanics import MechanicsError, ability_modifier, get_character_mechanics
from app.threads.service import can_read_thread, parse_thread_id
from app.visibility.access import is_campaign_participant
from models.campaigns import Campaign, CampaignMember
from models.characters import Character
from models.combat import Encounter, EncounterParticipant
from models.dm import DmTurn, DmTurnAttempt, PlayerRollFulfillment, PlayerRollRequest
from models.world import WorldEntity

logger = logging.getLogger(__name__)

ENCOUNTER_STARTED_EVENT = "encounter.started"
ENCOUNTER_READY_EVENT = "encounter.initiative_ready"
ENCOUNTER_ENDED_EVENT = "encounter.ended"
# Turn progression events — issue #231. Thread-scoped like the lifecycle
# events above; see THREAD_SCOPED_EVENT_TYPES.
TURN_STARTED_EVENT = "encounter.turn_started"
TURN_ENDED_EVENT = "encounter.turn_ended"
TURN_SKIPPED_EVENT = "encounter.turn_skipped"

MAX_PARTICIPANTS = 20

# 2024 rules tiebreak, deterministic DM decision as the final breaker:
# total desc, dex modifier desc, then stable participant-id order. Recorded
# on the encounter so ordering is auditable, never model output.
TIEBREAK_POLICY = "total_desc,dex_desc,participant_id_asc"


class EncounterError(ValueError):
    """Deterministic validation failure — caller must block, never guess."""


class EncounterAuthorizationError(PermissionError):
    pass


class EncounterNotReadyError(EncounterError):
    """Turn order is not authoritative while initiative is incomplete."""


class EncounterAlreadyActiveError(EncounterError):
    def __init__(self, campaign_id: uuid.UUID, active_id: uuid.UUID):
        self.campaign_id = campaign_id
        self.active_id = active_id
        super().__init__(
            f"Campaign {campaign_id} already has a pending/active encounter {active_id}; "
            "resolve it before starting a new one"
        )


def lock_encounter(db: Session, encounter_id: uuid.UUID, error: Callable[[str], Exception]) -> Encounter:
    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise error(f"Encounter {encounter_id} not found")
    return encounter


def lock_playable_campaign(db: Session, campaign_id: uuid.UUID, error: Callable[[str], Exception]) -> Campaign:
    campaign = db.execute(
        select(Campaign).where(Campaign.id == campaign_id).with_for_update()
    ).scalars().first()
    if campaign is None:
        raise error(f"Campaign {campaign_id} not found")
    require_playable_campaign(campaign)
    return campaign


def is_campaign_owner(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    campaign = db.get(Campaign, campaign_id)
    return campaign is not None and str(campaign.owner_id) == str(user_id)


# ── Stat resolution (code-owned, never guessed) ─────────────────────────────


def _resolve_pc_stats(db: Session, character_id: uuid.UUID) -> tuple[int, int, dict]:
    """Initiative (modifier, dex) + stat source from the canonical sheet (#224)."""
    try:
        mechanics = get_character_mechanics(db, character_id)
    except MechanicsError as exc:
        raise EncounterError(f"PC initiative blocked: invalid sheet ({exc})") from exc
    initiative = mechanics.combat["initiative"]
    meta = mechanics.meta
    source = {
        "source_type": "dnd5e_character_sheet",
        "source_id": meta.sheet_id,
        "source_version": meta.sheet_version,
        "rules_revision": meta.rules_revision,
    }
    return int(initiative["modifier"]), int(initiative["dex_modifier"]), source


def _resolve_npc_stats(
    db: Session, campaign_id: uuid.UUID, entity_id: uuid.UUID, override: int | None
) -> tuple[int, int, dict, str]:
    """NPC/monster initiative modifier from canonical entity details or DM selection.

    Precedence: explicit DM selection override > entity details
    (initiative_modifier, else dexterity score / dex_modifier + bonus) > 0.
    NPC/monster stats are always DM-private.
    """
    entity = db.get(WorldEntity, entity_id)
    if entity is None or str(entity.campaign_id) != str(campaign_id):
        raise EncounterError(f"NPC entity {entity_id} not found in this campaign")
    if (entity.entity_type or "").strip().lower() not in ("npc", "monster"):
        raise EncounterError(f"Entity {entity_id} is not an NPC/monster and cannot join combat")
    # NPC/monster combat stats are always DM-private per #230 security.
    visibility = "dm_private"
    details = entity.details or {}
    # Canonical Dexterity always resolves from entity details: an override
    # replaces only the total initiative modifier used in arithmetic, never
    # the Dexterity tiebreak input for deterministic 2024 ordering.
    dex_mod = 0
    if details.get("dex_modifier") is not None:
        try:
            dex_mod = int(details["dex_modifier"])
        except (TypeError, ValueError) as exc:
            raise EncounterError(f"NPC {entity_id} has a malformed dex_modifier") from exc
    elif details.get("dexterity") is not None:
        try:
            dex_mod = ability_modifier(int(details["dexterity"]))
        except (TypeError, ValueError) as exc:
            raise EncounterError(f"NPC {entity_id} has a malformed dexterity score") from exc
    if override is not None:
        try:
            modifier = int(override)
        except (TypeError, ValueError) as exc:
            raise EncounterError("npc initiative_modifier override must be an integer") from exc
        if not -20 <= modifier <= 20:
            raise EncounterError("npc initiative_modifier override must be between -20 and 20")
        return modifier, dex_mod, {
            "source_type": "dm_selection_override",
            "source_id": str(entity.id),
            "source_version": entity.updated_at.isoformat() if entity.updated_at else "unknown",
        }, visibility
    if details.get("initiative_modifier") is not None:
        try:
            modifier = int(details["initiative_modifier"])
        except (TypeError, ValueError) as exc:
            raise EncounterError(f"NPC {entity_id} has a malformed initiative_modifier") from exc
        return modifier, dex_mod, {
            "source_type": "world_entity_details",
            "source_id": str(entity.id),
            "source_version": entity.updated_at.isoformat() if entity.updated_at else "unknown",
        }, visibility
    bonus = details.get("initiative_bonus", 0) or 0
    try:
        bonus = int(bonus)
    except (TypeError, ValueError) as exc:
        raise EncounterError(f"NPC {entity_id} has a malformed initiative_bonus") from exc
    return dex_mod + bonus, dex_mod, {
        "source_type": "world_entity_details",
        "source_id": str(entity.id),
        "source_version": entity.updated_at.isoformat() if entity.updated_at else "unknown",
    }, visibility


# ── Selection validation ────────────────────────────────────────────────────


def _validate_selection(db: Session, campaign_id: uuid.UUID, participants: list[dict]) -> list[dict]:
    """Validate DM-selected participants; never auto-include unlisted PCs."""
    if not participants or len(participants) > MAX_PARTICIPANTS:
        raise EncounterError(f"participants must contain between 1 and {MAX_PARTICIPANTS} entries")
    seen_keys: set[str] = set()
    resolved: list[dict] = []
    for index, raw in enumerate(participants):
        if not isinstance(raw, dict):
            raise EncounterError(f"participant {index} must be an object")
        char_raw = raw.get("character_id")
        npc_raw = raw.get("npc_entity_id")
        if bool(char_raw) == bool(npc_raw):
            raise EncounterError(
                f"participant {index} must specify exactly one of character_id or npc_entity_id"
            )
        display = str(raw.get("display_name") or "").strip() or None
        if display is not None and len(display) > 160:
            raise EncounterError(f"participant {index} display_name must be at most 160 characters")
        if char_raw:
            try:
                character_id = uuid.UUID(str(char_raw))
            except ValueError as exc:
                raise EncounterError(f"participant {index} character_id must be a UUID") from exc
            character = db.get(Character, character_id)
            if character is None:
                raise EncounterError(f"participant {index} character {character_id} not found")
            campaign = db.get(Campaign, campaign_id)
            if campaign is None or not is_campaign_participant(db, campaign, character.owner_id):
                raise EncounterError(
                    f"participant {index} character owner is not a member of this campaign"
                )
            # Roster scoping (#266 canonical): the character must be on the
            # campaign's active roster — selected by a member — so an owner
            # cannot enroll a member's unrelated character. Terminal lifecycle
            # states (dead/retired) can never join; a missing row means active.
            roster = db.execute(
                select(CampaignMember).where(
                    CampaignMember.campaign_id == campaign_id,
                    CampaignMember.selected_character_id == character_id,
                )
            ).scalars().first()
            if roster is None:
                raise EncounterError(
                    f"participant {index} character is not on this campaign's active roster"
                )
            lifecycle = get_lifecycle(db, campaign_id, character_id)
            if lifecycle is not None and lifecycle.status in TERMINAL_PC_STATUSES:
                raise EncounterError(
                    f"participant {index} character is {lifecycle.status} and cannot join combat"
                )
            modifier, dex_mod, source = _resolve_pc_stats(db, character_id)
            key = f"pc:{character_id}"
            if key in seen_keys:
                raise EncounterError(f"duplicate participant for character {character_id}")
            seen_keys.add(key)
            resolved.append({
                "participant_key": key, "kind": "pc", "character_id": character_id,
                "npc_entity_id": None, "controller_user_id": character.owner_id,
                "display_name": display or character.name,
                "initiative_modifier": modifier, "dex_modifier": dex_mod,
                "stat_source": source, "stat_visibility": "public",
            })
        else:
            try:
                entity_id = uuid.UUID(str(npc_raw))
            except ValueError as exc:
                raise EncounterError(f"participant {index} npc_entity_id must be a UUID") from exc
            modifier, dex_mod, source, visibility = _resolve_npc_stats(
                db, campaign_id, entity_id, raw.get("initiative_modifier")
            )
            entity = db.get(WorldEntity, entity_id)
            entity_type = (entity.entity_type or "npc").strip().lower()
            kind = "monster" if entity_type == "monster" else "npc"
            key = f"{kind}:{entity_id}"
            if key in seen_keys:
                # Same entity fielded twice (e.g. two goblins): disambiguate.
                suffix = 2
                while f"{key}:{suffix}" in seen_keys:
                    suffix += 1
                key = f"{key}:{suffix}"
            seen_keys.add(key)
            resolved.append({
                "participant_key": key, "kind": kind, "character_id": None,
                "npc_entity_id": entity_id, "controller_user_id": None,
                "display_name": display or entity.name,
                "initiative_modifier": modifier, "dex_modifier": dex_mod,
                "stat_source": source, "stat_visibility": visibility,
            })
    return resolved


def _validate_scene(db: Session, campaign_id: uuid.UUID, scene: dict | None) -> dict:
    scene = scene or {}
    if not isinstance(scene, dict):
        raise EncounterError("scene must be an object")
    location_entity_id = scene.get("location_entity_id")
    if location_entity_id is not None:
        try:
            location_entity_id = uuid.UUID(str(location_entity_id))
        except ValueError as exc:
            raise EncounterError("scene location_entity_id must be a UUID") from exc
        entity = db.get(WorldEntity, location_entity_id)
        if entity is None or str(entity.campaign_id) != str(campaign_id):
            raise EncounterError("scene location entity not found in this campaign")
    location_name = scene.get("location_name")
    if location_name is not None:
        location_name = str(location_name).strip() or None
        if location_name is not None and len(location_name) > 256:
            raise EncounterError("scene location_name must be at most 256 characters")
    map_ref = scene.get("map_ref")
    if map_ref is not None:
        map_ref = str(map_ref).strip() or None
        if map_ref is not None and len(map_ref) > 256:
            raise EncounterError("scene map_ref must be at most 256 characters")
    return {
        "location_entity_id": location_entity_id,
        "location_name": location_name,
        "map_ref": map_ref,
    }


# ── Reads ───────────────────────────────────────────────────────────────────


def get_encounter(db: Session, encounter_id: uuid.UUID) -> Encounter | None:
    return db.get(Encounter, encounter_id)


def get_active_encounter(db: Session, campaign_id: uuid.UUID) -> Encounter | None:
    """Latest pending/active encounter for a campaign (durable reconnect read)."""
    return db.execute(
        select(Encounter)
        .where(Encounter.campaign_id == campaign_id, Encounter.status.in_(["pending_initiative", "active"]))
        .order_by(Encounter.created_at.desc(), Encounter.id.desc())
        .limit(1)
    ).scalars().first()


def list_participants(db: Session, encounter_id: uuid.UUID) -> list[EncounterParticipant]:
    return list(db.execute(
        select(EncounterParticipant)
        .where(EncounterParticipant.encounter_id == encounter_id)
        .order_by(EncounterParticipant.created_at.asc(), EncounterParticipant.id.asc())
    ).scalars().all())


def find_by_operation(db: Session, campaign_id: uuid.UUID, operation_id: str) -> Encounter | None:
    if not operation_id:
        return None
    return db.execute(
        select(Encounter).where(
            Encounter.campaign_id == campaign_id, Encounter.operation_id == operation_id
        )
    ).scalars().first()


def find_created_event(db: Session, encounter: Encounter):
    """Resolve the encounter-start domain event: stored id, else operation lookup."""
    from models.campaigns import CampaignDomainEvent

    if encounter.created_event_id is not None:
        event = db.get(CampaignDomainEvent, encounter.created_event_id)
        if event is not None:
            return event
    if encounter.operation_id:
        return db.execute(
            select(CampaignDomainEvent).where(
                CampaignDomainEvent.campaign_id == encounter.campaign_id,
                CampaignDomainEvent.operation_id == encounter.operation_id,
                CampaignDomainEvent.event_type == ENCOUNTER_STARTED_EVENT,
            )
        ).scalars().first()
    return None


# ── Deterministic 2024 initiative ordering ──────────────────────────────────


def compute_turn_order(participants: list[EncounterParticipant]) -> tuple[list[uuid.UUID], int]:
    """Order fulfilled participants: total desc, dex desc, id asc.

    Returns (ordered ids, tied-group count). Every participant must carry a
    total; callers gate on completeness first so human rolls are never guessed.
    """
    fulfilled = [p for p in participants if p.initiative_total is not None]
    if len(fulfilled) != len(participants):
        raise EncounterNotReadyError("turn order requires every participant's initiative roll")
    ordered = sorted(
        fulfilled,
        key=lambda p: (-int(p.initiative_total), -int(p.dex_modifier), str(p.id)),
    )
    tied_groups = 0
    index = 0
    while index < len(ordered):
        group = [ordered[index]]
        cursor = index + 1
        while (
            cursor < len(ordered)
            and int(ordered[cursor].initiative_total) == int(ordered[index].initiative_total)
            and int(ordered[cursor].dex_modifier) == int(ordered[index].dex_modifier)
        ):
            group.append(ordered[cursor])
            cursor += 1
        if len(group) > 1:
            tied_groups += 1
            for member in group:
                member.is_tied = True
        else:
            ordered[index].is_tied = False
        index = cursor
    return [p.id for p in ordered], tied_groups


def get_turn_order(db: Session, encounter_id: uuid.UUID) -> list[EncounterParticipant]:
    """Authoritative order; raises while initiative is incomplete."""
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise EncounterError(f"Encounter {encounter_id} not found")
    if encounter.status != "active" or not encounter.turn_order_ids:
        raise EncounterNotReadyError("turn order becomes authoritative only when required initiative is complete")
    participants = {str(p.id): p for p in list_participants(db, encounter_id)}
    ordered: list[EncounterParticipant] = []
    for pid in (encounter.turn_order_ids or []):
        participant = participants.get(str(pid))
        if participant is None:
            raise EncounterError(f"turn order references unknown participant {pid}")
        ordered.append(participant)
    return ordered


def get_active_participant(db: Session, encounter_id: uuid.UUID) -> EncounterParticipant:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise EncounterError(f"Encounter {encounter_id} not found")
    if encounter.status != "active" or encounter.active_participant_id is None:
        raise EncounterNotReadyError("no active turn until required initiative is complete")
    participant = db.get(EncounterParticipant, encounter.active_participant_id)
    if participant is None:
        raise EncounterError("active participant no longer exists")
    return participant


# ── Viewer-filtered projection (snapshot + reads; hidden NPC stats DM-private)


def encounter_view(db: Session, encounter: Encounter, viewer_id: uuid.UUID) -> dict:
    """Player-facing encounter projection.

    The AI is the only DM, so every human — the campaign owner included —
    gets the same view: private stats only for participants they control,
    no DM end reason, no hidden NPC fates, no hidden tokens or DM terrain.
    """
    payload = encounter.to_dict()
    created_event = find_created_event(db, encounter)
    if created_event is not None:
        payload["created_event_id"] = str(created_event.id)
        payload["created_event_sequence"] = created_event.sequence
    viewers_parts = []
    hidden_ids: set[str] = set()
    for participant in list_participants(db, encounter.id):
        include_private = (
            participant.controller_user_id is not None
            and str(participant.controller_user_id) == str(viewer_id)
        )
        item = participant.to_dict(include_private=include_private)
        if not include_private:
            item.pop("roll_request_id", None)
        viewers_parts.append(item)
        # Issue #239: DM-authored end reason/fates may name hidden NPC fates
        # (end_encounter staged effects are dm_private for this reason).
        # Track hidden combatants so the projection below can redact their fate entries while keeping PC/public outcomes.
        if (
            participant.kind in ("npc", "monster")
            and participant.stat_visibility == "dm_private"
            and not include_private
        ):
            hidden_ids.add(str(participant.id))
    payload["participants"] = viewers_parts
    payload["end_reason"] = None
    outcomes = payload.get("end_participant_outcomes") or {}
    if isinstance(outcomes, dict) and hidden_ids:
        payload["end_participant_outcomes"] = {
            pid: fate for pid, fate in outcomes.items() if str(pid) not in hidden_ids
        }
    # Issue #231: durable turn/round/resource projection rides the same
    # snapshot so reconnects reconstruct mechanical state exactly. Lazy
    # import: turns.py owns these helpers and imports this module.
    try:
        from app.combat.turns import turn_projection

        payload["turn"] = turn_projection(db, encounter)
    except Exception:
        logger.warning("encounter turn projection skipped", exc_info=True)
        payload["turn"] = None
    # Issue #232: authoritative map/placement projection rides the same
    # snapshot so reconnects reconstruct positions and terrain exactly.
    try:
        from app.combat.maps import map_projection

        payload["map"] = map_projection(db, encounter, viewer_id=viewer_id)
    except Exception:
        logger.warning("encounter map projection skipped", exc_info=True)
        payload["map"] = None
    return payload


def can_view_encounter(db: Session, encounter: Encounter, viewer_id: uuid.UUID) -> bool:
    """Thread-scoped encounter visibility — issue #230 privacy.

    The encounter inherits its source turn's thread; a private-thread
    encounter is visible only to readers of that thread (the repository's
    central thread invariant: owner status alone never grants private
    access). Unparseable/missing threads fail closed.
    """
    try:
        thread_id = parse_thread_id(encounter.thread_id)
    except Exception:
        return False
    try:
        return bool(can_read_thread(db, encounter.campaign_id, thread_id, viewer_id))
    except Exception:
        return False


#: Encounter lifecycle events inherit the source turn's thread. The member
#: event feed enforces the same thread boundary as ``can_view_encounter``
#: so private-thread combat metadata never leaks through ``/events``
#: history (issue #230 privacy). Pagination over the authorized stream is
#: deferred to the thread/privacy work — hidden events may leave short
#: pages, but filtering is deterministic per row so pages never duplicate.
THREAD_SCOPED_EVENT_TYPES = frozenset({
    ENCOUNTER_STARTED_EVENT, ENCOUNTER_READY_EVENT, ENCOUNTER_ENDED_EVENT,
    TURN_STARTED_EVENT, TURN_ENDED_EVENT, TURN_SKIPPED_EVENT,
    # Issue #232: movement/map mutations inherit the encounter's source
    # thread under the same read boundary (literals avoid a maps import
    # cycle; canonical names live in app.combat.maps).
    "encounter.map_updated", "encounter.moved",
})


def encounter_event_visible_to(db: Session, event, viewer_id: uuid.UUID) -> bool:
    """Whether a lifecycle domain event may appear in a member's event feed.

    Non-thread-scoped types always pass (callers pre-filter those). Scoped
    types require source-thread readability; missing/unparseable thread
    discriminators fail closed.
    """
    if event.event_type not in THREAD_SCOPED_EVENT_TYPES:
        return True
    payload = getattr(event, "payload", None)
    thread_ref = payload.get("thread_id") if isinstance(payload, dict) else None
    if not thread_ref:
        return False
    try:
        thread_id = parse_thread_id(thread_ref)
    except Exception:
        return False
    try:
        return bool(can_read_thread(db, event.campaign_id, thread_id, viewer_id))
    except Exception:
        return False


def get_snapshot_encounter(db: Session, campaign_id: uuid.UUID, viewer_id: uuid.UUID) -> dict | None:
    """Reconnect-safe encounter projection for the live-table snapshot."""
    campaign = db.get(Campaign, campaign_id)
    if campaign is None:
        return None
    if not is_campaign_participant(db, campaign, viewer_id):
        return None
    encounter = get_active_encounter(db, campaign_id)
    if encounter is None:
        return None
    if not can_view_encounter(db, encounter, viewer_id):
        return None
    view = encounter_view(db, encounter, viewer_id)
    mine = [
        p["id"] for p in view["participants"]
        if p.get("controller_user_id") == str(viewer_id) and p.get("initiative_status") == "pending"
    ]
    view["my_pending_initiative"] = mine
    return view


# ── Core builder (DM structured-effect path) ─────────────────────────────────


def _build_encounter_rows(
    db: Session,
    campaign: Campaign,
    turn: DmTurn,
    attempt_id: uuid.UUID,
    *,
    operation_id: str,
    scene: dict | None,
    participants: list[dict],
) -> Encounter:
    resolved = _validate_selection(db, campaign.id, participants)
    # Thread-scoped audience (#230 privacy): a human controller who cannot
    # read the encounter's source thread would receive an initiative request
    # they can never see, leaving combat permanently pending. Reject up
    # front instead of persisting an unfulfillable combatant.
    try:
        encounter_thread_id = parse_thread_id(turn.thread_id)
    except Exception as exc:
        raise EncounterError("source turn has an invalid thread and cannot start combat") from exc
    for item in resolved:
        controller = item.get("controller_user_id")
        if item["kind"] != "pc" or controller is None:
            continue
        try:
            readable = can_read_thread(db, campaign.id, encounter_thread_id, controller)
        except Exception:
            readable = False
        if not readable:
            raise EncounterError(
                "selected character's controller cannot read the encounter thread"
            )
    scene_parts = _validate_scene(db, campaign.id, scene)
    encounter = Encounter(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        thread_id=turn.thread_id,
        status="pending_initiative",
        round=1,
        active_index=0,
        revision=1,
        scene_location_entity_id=scene_parts["location_entity_id"],
        scene_location_name=scene_parts["location_name"],
        map_ref=scene_parts["map_ref"],
        start_source="dm_effect",
        source_turn_id=turn.id,
        source_attempt_id=attempt_id,
        operation_id=operation_id,
        participant_count=len(resolved),
        initiated_at=utcnow(),
    )
    db.add(encounter)
    db.flush()
    short = encounter.id.hex[:8]
    for index, item in enumerate(resolved):
        participant = EncounterParticipant(
            id=uuid.uuid4(),
            encounter_id=encounter.id,
            campaign_id=campaign.id,
            participant_key=item["participant_key"],
            kind=item["kind"],
            character_id=item["character_id"],
            npc_entity_id=item["npc_entity_id"],
            controller_user_id=item["controller_user_id"],
            display_name=item["display_name"],
            initiative_modifier=item["initiative_modifier"],
            dex_modifier=item["dex_modifier"],
            stat_source=item["stat_source"],
            stat_visibility=item["stat_visibility"],
            initiative_status="pending",
        )
        db.add(participant)
        db.flush()
        if item["kind"] == "pc":
            request_key = f"init-{short}-{index}"
            roll_request = PlayerRollRequest(
                campaign_id=campaign.id,
                thread_id=turn.thread_id,
                turn_id=turn.id,
                attempt_id=attempt_id,
                request_key=request_key,
                requested_user_id=item["controller_user_id"],
                character_id=item["character_id"],
                roll_kind="initiative",
                ability_or_skill="Initiative",
                label=f"Initiative — {item['display_name']}"[:120],
                advantage_state="normal",
                reason_public=f"Roll initiative for {item['display_name']} (combat encounter).",
                dc_private=None,
                status="pending",
            )
            db.add(roll_request)
            db.flush()
            participant.roll_request_id = roll_request.id
            db.flush()
        else:
            # NPC/monster initiative is a DM/runtime roll: code rolls it at
            # start. Humans always roll their own (the request above).
            _roll_npc_inline(participant, raw_d20=None)
    db.flush()
    return encounter


def _parse_d20(raw: Any) -> int:
    """Bounded d20 parser shared by initial rolls and idempotent replays."""
    try:
        die = int(raw)
    except (TypeError, ValueError) as exc:
        raise EncounterError("raw_d20 must be an integer between 1 and 20") from exc
    if not 1 <= die <= 20:
        raise EncounterError("raw_d20 must be between 1 and 20")
    return die


def _roll_npc_inline(participant: EncounterParticipant, *, raw_d20: int | None) -> EncounterParticipant:
    if participant.kind not in ("npc", "monster"):
        raise EncounterError("runtime rolls are for NPC/monster participants only; humans roll their own initiative")
    if participant.initiative_status == "fulfilled":
        return participant
    if raw_d20 is None:
        raw_d20 = secrets.randbelow(20) + 1
    die = _parse_d20(raw_d20)
    participant.raw_roll = die
    participant.initiative_total = die + int(participant.initiative_modifier)
    participant.roll_source = "dm_runtime"
    participant.initiative_status = "fulfilled"
    participant.fulfilled_at = utcnow()
    return participant


def _maybe_mark_ready(db: Session, campaign: Campaign, encounter: Encounter, *, commit: bool) -> Any | None:
    """Transition to active when every required initiative roll exists.

    Never invents human rolls: any pending participant keeps the encounter
    pending. Flush-only when commit=False; the caller owns the transaction.
    """
    participants = list_participants(db, encounter.id)
    if any(p.initiative_status != "fulfilled" or p.initiative_total is None for p in participants):
        return None
    ordered_ids, tied_groups = compute_turn_order(participants)
    for order, pid in enumerate(ordered_ids):
        member = db.get(EncounterParticipant, pid)
        member.sort_order = order
    ready_at = utcnow()
    wait_ms = ms_between(encounter.initiated_at, ready_at)
    encounter.status = "active"
    encounter.round = 1
    encounter.active_index = 0
    encounter.active_participant_id = ordered_ids[0]
    encounter.revision = int(encounter.revision or 1) + 1
    encounter.turn_order_ids = [str(pid) for pid in ordered_ids]
    encounter.tie_resolution = (
        f"{TIEBREAK_POLICY}; participants={len(ordered_ids)}; tied_groups={tied_groups}"
    )
    encounter.ready_at = ready_at
    encounter.initiative_wait_ms = wait_ms
    encounter.time_to_first_turn_ms = wait_ms
    encounter.roll_sources = {str(p.id): p.roll_source for p in participants}
    # Issue #231: the first mechanical turn opens here — sequence 1 with full
    # per-participant budgets. Resource init never blocks readiness: speed
    # resolution falls back to 30 ft rather than failing the encounter.
    encounter.turn_sequence = 1
    encounter.turn_started_at = ready_at
    encounter.blocked_since = None
    db.flush()

    from app.combat.turns import init_turn_states

    init_turn_states(db, encounter, now=ready_at)

    # Bounded stable transition key: the start operation_id may legally fill
    # the 128-char column, so suffixing it would overflow on PostgreSQL.
    ready_operation_id = f"encounter:{encounter.id}:initiative-ready"
    _, event = commit_campaign_mutation(
        db,
        campaign.id,
        expected_revision=int(campaign.revision or 0),
        event_type=ENCOUNTER_READY_EVENT,
        payload={
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "turn_order_ids": [str(pid) for pid in ordered_ids],
            "active_participant_id": str(ordered_ids[0]),
            "round": 1,
            "participant_count": len(ordered_ids),
            "tie_resolution": encounter.tie_resolution,
            "roll_sources": dict(encounter.roll_sources or {}),
            "time_to_first_turn_ms": wait_ms,
        },
        operation_id=ready_operation_id,
        actor_id=campaign.owner_id,
        commit=commit,
    )
    structured_log(
        logger, logging.INFO, "encounter_ready",
        encounter_id=str(encounter.id), campaign_id=str(campaign.id),
        participant_count=len(ordered_ids), tied_groups=tied_groups,
        tie_resolution=encounter.tie_resolution,
        initiative_wait_ms=wait_ms, time_to_first_turn_ms=wait_ms,
        roll_sources=dict(encounter.roll_sources or {}),
    )
    # Issue #231: the first mechanical turn opens atomically with readiness —
    # same transaction, next campaign revision — so turn duration tracking
    # and reconnect reads never observe an active encounter without a turn.
    first_turn_operation_id = f"encounter:{encounter.id}:turn:1:started"
    commit_campaign_mutation(
        db,
        campaign.id,
        expected_revision=int(campaign.revision or 0),
        event_type=TURN_STARTED_EVENT,
        payload={
            "encounter_id": str(encounter.id),
            "thread_id": encounter.thread_id,
            "turn_sequence": 1,
            "active_participant_id": str(ordered_ids[0]),
            "round": 1,
            "rolled_over": False,
        },
        operation_id=first_turn_operation_id,
        actor_id=campaign.owner_id,
        commit=commit,
    )
    return event


def start_encounter_inline(
    db: Session,
    campaign: Campaign,
    turn: DmTurn,
    attempt: DmTurnAttempt,
    args: dict,
    operation_key: str,
) -> Encounter:
    """DM structured-effect path: build rows inside the caller's turn-commit txn.

    No commit here — the outer turn commit owns both. The caller's
    ``commit_turn`` stages the distinct ``encounter.started`` lifecycle
    event in the same outer transaction and binds it as the
    start provenance (resolved on read via operation_id).
    Duplicate effect replays return the existing encounter.
    """
    prior = find_by_operation(db, campaign.id, operation_key)
    if prior is not None:
        logger.info(
            "encounter inline_duplicate_hit campaign_id=%s encounter_id=%s op=%s",
            campaign.id, prior.id, operation_key,
        )
        return prior
    active = get_active_encounter(db, campaign.id)
    if active is not None:
        raise EncounterAlreadyActiveError(campaign.id, active.id)
    encounter = _build_encounter_rows(
        db, campaign, turn, attempt.id, operation_id=operation_key,
        scene=args.get("scene") if isinstance(args, dict) else None,
        participants=(args.get("participants") if isinstance(args, dict) else None) or [],
    )
    map_spec = args.get("map") if isinstance(args, dict) else None
    if map_spec:
        from app.combat.maps import init_map_inline

        init_map_inline(db, encounter, map_spec)
    structured_log(
        logger, logging.INFO, "encounter_started",
        encounter_id=str(encounter.id), campaign_id=str(campaign.id),
        start_source="dm_effect", participant_count=encounter.participant_count,
        operation_id=operation_key, source_turn_id=str(turn.id),
    )
    return encounter


def fulfill_human_initiative(
    db: Session,
    encounter_id: uuid.UUID,
    participant_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    payload: dict,
    commit: bool = True,
) -> tuple[PlayerRollRequest, PlayerRollFulfillment, EncounterParticipant, Encounter, Any | None]:
    """Human-PC initiative fulfillment: the human rolls, code validates + totals.

    Mirrors the #204 fulfillment contract (owner-only, app/physical source,
    idempotent single fulfillment) but resumes the encounter instead of a DM
    turn: no turn-resume side effects.
    """
    started = utcnow()
    encounter = db.execute(
        select(Encounter).where(Encounter.id == encounter_id).with_for_update()
    ).scalars().first()
    if encounter is None:
        raise EncounterError(f"Encounter {encounter_id} not found")
    require_playable_campaign(lock_campaign_row(db, encounter.campaign_id))
    if encounter.status != "pending_initiative":
        raise EncounterError(f"encounter cannot accept initiative from status {encounter.status}")
    participant = db.execute(
        select(EncounterParticipant).where(EncounterParticipant.id == participant_id).with_for_update()
    ).scalars().first()
    if participant is None or participant.encounter_id != encounter.id:
        raise EncounterError("participant not found in this encounter")
    if participant.kind != "pc":
        raise EncounterError("NPC/monster initiative uses the DM runtime roll path, not human fulfillment")
    if participant.controller_user_id is None or str(participant.controller_user_id) != str(actor_id):
        logger.warning(
            "encounter_initiative invalid_attempt encounter_id=%s participant_id=%s actor_id=%s reason=unauthorized",
            encounter_id, participant_id, actor_id,
        )
        raise EncounterAuthorizationError("Only the PC's controller may roll its initiative")
    character = db.get(Character, participant.character_id) if participant.character_id else None
    if character is None or str(character.owner_id) != str(actor_id):
        raise EncounterAuthorizationError("Character control changed; initiative cannot be fulfilled")
    if participant.initiative_status != "pending":
        raise EncounterError(f"initiative cannot be fulfilled from status {participant.initiative_status}")
    request = db.execute(
        select(PlayerRollRequest).where(PlayerRollRequest.id == participant.roll_request_id).with_for_update()
    ).scalars().first() if participant.roll_request_id else None
    if request is None or request.status != "pending":
        raise EncounterError("initiative roll request is no longer pending")

    source = str(payload.get("source") or "")
    if source not in ("app", "physical"):
        raise EncounterError("source must be app or physical")
    raw_rolls = payload.get("raw_rolls", [])
    if (
        not isinstance(raw_rolls, list) or len(raw_rolls) != 1
        or type(raw_rolls[0]) is not int or not 1 <= raw_rolls[0] <= 20
    ):
        raise EncounterError("initiative requires exactly one d20 raw_roll between 1 and 20")
    try:
        modifier = int(payload.get("modifier", 0))
        total = int(payload["total"])
    except (KeyError, TypeError, ValueError) as exc:
        raise EncounterError("modifier and total must be integers; total is required") from exc
    # Deterministic authority: arithmetic stays in code, never trusted blindly.
    if modifier != int(participant.initiative_modifier):
        raise EncounterError(
            f"modifier {modifier} does not match the canonical sheet modifier {participant.initiative_modifier}"
        )
    expected_total = int(raw_rolls[0]) + int(participant.initiative_modifier)
    if total != expected_total:
        raise EncounterError(f"total {total} must equal d20 {raw_rolls[0]} + modifier {participant.initiative_modifier}")
    visibility = str(payload.get("visibility") or "public")
    if visibility not in ("public", "private"):
        raise EncounterError("visibility must be public or private")
    metadata = payload.get("raw_metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise EncounterError("raw_metadata must be an object")

    fulfillment = PlayerRollFulfillment(
        roll_request_id=request.id, submitted_by=actor_id, source=source, visibility=visibility,
        raw_rolls=raw_rolls, modifier=modifier, total=total, raw_metadata=metadata,
    )
    db.add(fulfillment)
    request.status = "fulfilled"
    request.fulfilled_at = utcnow()
    participant.raw_roll = int(raw_rolls[0])
    participant.initiative_total = total
    participant.roll_source = "human_app" if source == "app" else "human_physical"
    participant.initiative_status = "fulfilled"
    participant.fulfilled_at = utcnow()
    db.flush()

    campaign = db.get(Campaign, encounter.campaign_id)
    event = _maybe_mark_ready(db, campaign, encounter, commit=False)
    if commit:
        db.commit()
        db.refresh(request)
        db.refresh(fulfillment)
        db.refresh(participant)
        db.refresh(encounter)
    wait_ms = ms_between(encounter.initiated_at, utcnow())
    structured_log(
        logger, logging.INFO, "encounter_initiative_fulfilled",
        encounter_id=str(encounter.id), participant_id=str(participant.id),
        roll_source=participant.roll_source, total=total,
        initiative_wait_ms=wait_ms,
        latency_ms=round((utcnow() - started).total_seconds() * 1000, 2),
    )
    if commit and event is not None:
        publish_encounter_ready(db, encounter)
    return request, fulfillment, participant, encounter, event


# ── DM-turn lifecycle staging (issues #230, #239) ───────────────────────────


def _turn_lifecycle_event(db: Session, campaign_id: uuid.UUID, operation_id, event_type: str):
    from models.campaigns import CampaignDomainEvent

    return db.execute(
        select(CampaignDomainEvent).where(
            CampaignDomainEvent.campaign_id == campaign_id,
            CampaignDomainEvent.operation_id == operation_id,
            CampaignDomainEvent.event_type == event_type,
        )
    ).scalars().first()


def stage_turn_encounter_events(
    db: Session, *, turn: DmTurn, attempt: DmTurnAttempt, turn_event, campaign_after: Campaign,
) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Stage encounter lifecycle events for encounters this attempt started/ended.

    The turn commit IS the fictional mutation of a ``start_encounter`` /
    ``end_encounter`` effect, so each affected encounter gets its own domain
    event chained in the same outer transaction (one event per revision,
    preserving the sequence == revision invariant). Fail-closed: a staging
    failure propagates and aborts the turn commit — a durable start or end
    without its lifecycle event must never commit. Returns the (started,
    ended) encounter ids for post-commit realtime delivery.
    """
    provenance = {
        "source": "dm_effect",
        "turn_event_id": str(turn_event.id),
        "attempt_id": str(attempt.id),
    }
    started_ids: list[uuid.UUID] = []
    started = db.execute(
        select(Encounter).where(
            Encounter.campaign_id == turn.campaign_id,
            Encounter.source_attempt_id == attempt.id,
            Encounter.created_event_id.is_(None),
        )
    ).scalars().all()
    for encounter in started:
        lifecycle = _turn_lifecycle_event(
            db, turn.campaign_id, encounter.operation_id, ENCOUNTER_STARTED_EVENT)
        if lifecycle is None:
            _, lifecycle = commit_campaign_mutation(
                db,
                turn.campaign_id,
                expected_revision=int(campaign_after.revision or 0),
                event_type=ENCOUNTER_STARTED_EVENT,
                payload={
                    "encounter_id": str(encounter.id),
                    "thread_id": encounter.thread_id,
                    "participant_count": int(encounter.participant_count or 0),
                    "start_source": encounter.start_source,
                    "source_turn_id": str(turn.id),
                    "source_attempt_id": str(attempt.id),
                    "participants": [
                        {"id": str(p.id), "kind": p.kind, "display_name": p.display_name}
                        for p in list_participants(db, encounter.id)
                    ],
                },
                operation_id=encounter.operation_id,
                actor_id=turn_event.actor_id,
                provenance=provenance,
                commit=False,
            )
        encounter.created_event_id = lifecycle.id
        started_ids.append(encounter.id)

    # Only encounters this attempt's own end_encounter effects targeted: the
    # API end path stages its event immediately, and a turn must never claim
    # provenance for an end it did not cause.
    end_targets: list[uuid.UUID] = []
    for effect in attempt.staged_effects or []:
        if effect.get("effect_type") != "end_encounter":
            continue
        try:
            end_targets.append(uuid.UUID(str((effect.get("arguments") or {}).get("encounter_id"))))
        except ValueError:
            continue
    ended_ids: list[uuid.UUID] = []
    ended = db.execute(
        select(Encounter).where(
            Encounter.campaign_id == turn.campaign_id,
            Encounter.id.in_(end_targets),
            Encounter.status == "ended",
            Encounter.ended_event_id.is_(None),
        )
    ).scalars().all() if end_targets else []
    if ended:
        from app.combat.ending import build_final_snapshot, list_end_followups

    for encounter in ended:
        lifecycle = _turn_lifecycle_event(
            db, turn.campaign_id, encounter.end_operation_id, ENCOUNTER_ENDED_EVENT)
        if lifecycle is None:
            _, lifecycle = commit_campaign_mutation(
                db,
                turn.campaign_id,
                expected_revision=int(campaign_after.revision or 0),
                event_type=ENCOUNTER_ENDED_EVENT,
                payload={
                    "encounter_id": str(encounter.id),
                    "thread_id": encounter.thread_id,
                    "outcome": encounter.end_outcome,
                    "reason": encounter.end_reason,
                    "round": int(encounter.round or 1),
                    "turn_sequence": int(encounter.turn_sequence or 0),
                    "duration_ms": int(encounter.end_duration_ms or 0),
                    "participant_outcomes": dict(encounter.end_participant_outcomes or {}),
                    "followup_hooks": [h.hook_type for h in list_end_followups(db, encounter.id)],
                    "final_state": build_final_snapshot(db, encounter),
                    "ended_by": str(encounter.ended_by) if encounter.ended_by else None,
                },
                operation_id=encounter.end_operation_id,
                # Issue #239 privacy: owner-only like the API path. The
                # turn event's actor may be the player whose turn triggered
                # the DM effect; the ended payload carries DM-private
                # reason/fates, so the campaign owner (AI-DM path) must own
                # the event or members would see it as own-actor.
                actor_id=campaign_after.owner_id,
                visibility="dm_only",
                provenance=provenance,
                commit=False,
            )
        encounter.ended_event_id = lifecycle.id
        ended_ids.append(encounter.id)
    if started_ids or ended_ids:
        db.flush()
    return started_ids, ended_ids


def publish_turn_encounter_events(
    db: Session, started_ids: list[uuid.UUID], ended_ids: list[uuid.UUID],
) -> None:
    """Post-commit realtime delivery for staged lifecycle events.

    The staged lifecycle events are authoritative; this direct publish is
    latency-only and best-effort — it never rolls back committed state.
    """
    for ids, publish, label in (
        (started_ids, publish_encounter_started, "start"),
        (ended_ids, publish_encounter_ended, "end"),
    ):
        if not ids:
            continue
        try:
            for encounter_id in ids:
                row = db.get(Encounter, encounter_id)
                if row is not None:
                    publish(db, row)
        except Exception as exc:
            logger.warning("encounter %s post-commit publish skipped error=%s", label, exc)
