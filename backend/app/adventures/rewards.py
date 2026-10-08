"""Adventure-closing XP awards — issue #261.

Code-owned rule (2024 rules, "Experience Points" for defeated monsters):

- The adventure's encounters are those whose ``encounter.ended`` event falls
  in the arc's event range (``start_sequence``..``end_sequence``), plus any
  ended by the completing DM turn itself (its ended event is staged right
  after the ``adventure.completed`` event, so it carries the completion
  event as ``provenance.turn_event_id``).
- In each encounter, every NPC/monster participant whose recorded fate is
  in :data:`DEFEATED_FATES` contributes its SRD stat block's XP. Participants
  still ``standing`` or that ``retreated`` in good order were not overcome
  and award nothing; NPCs without an SRD stat block have no CR and award 0.
  XP is re-read from the committed bestiary by ``monster_id``, never from a
  model-editable number.
- Each encounter's XP is divided evenly (floored) among the PCs that took
  part in it, fallen PCs included.
- The adventure outcome does not scale XP: a failed or fled arc still keeps
  the XP for foes it actually defeated, and a victory earns no extra bonus.

Exactly-once: one :class:`AdventureXpAward` row per (adventure, character)
under a unique key, written in the same transaction as the sheet update; an
encounter's ``xp_progression`` hook is completed when it is credited so a
later adventure cannot credit it again. Closing never touches HP,
conditions, inventory, clocks or world state, and never levels a character:
crossing an advancement threshold is only reported.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from models.campaigns import Adventure, AdventureXpAward, CampaignDomainEvent
from models.combat import Encounter, EncounterEndFollowup

logger = logging.getLogger(__name__)

#: 2024 PHB Character Advancement: minimum XP for levels 1..20.
XP_LEVEL_THRESHOLDS = (
    0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000, 64000,
    85000, 100000, 120000, 140000, 165000, 195000, 225000, 265000, 305000, 355000,
)

#: Participant fates (``app.combat.ending.PARTICIPANT_OUTCOMES``) that count
#: as the party overcoming that foe.
DEFEATED_FATES = frozenset({"slain", "unconscious", "surrendered", "captured", "fled"})

XP_HOOK = "xp_progression"


def level_for_xp(xp: int) -> int:
    """Highest level whose advancement threshold ``xp`` meets."""
    return max(i + 1 for i, floor in enumerate(XP_LEVEL_THRESHOLDS) if int(xp) >= floor)


def participant_xp(db: Session, participant) -> int:
    """SRD XP for one defeated NPC/monster participant (0 without a stat block)."""
    from app.rules.bestiary import STAT_BLOCK_SECTION, get_stat_block
    from models.world import WorldEntity

    if participant.npc_entity_id is None:
        return 0
    entity = db.get(WorldEntity, participant.npc_entity_id)
    details = entity.details if entity is not None and isinstance(entity.details, dict) else {}
    section = details.get(STAT_BLOCK_SECTION)
    if not isinstance(section, dict):
        return 0
    block = get_stat_block(section.get("monster_id"))
    return int(block["xp"]) if block is not None else 0


def adventure_encounters(db: Session, adventure: Adventure) -> list[Encounter]:
    """Ended encounters belonging to the adventure's event range, in end order."""
    start = int(adventure.start_sequence or 0)
    end = adventure.end_sequence
    completion_event = str(adventure.source_event_id) if adventure.source_event_id else None
    rows = db.execute(
        select(Encounter, CampaignDomainEvent)
        .join(CampaignDomainEvent, Encounter.ended_event_id == CampaignDomainEvent.id)
        .where(
            Encounter.campaign_id == adventure.campaign_id,
            Encounter.status == "ended",
            CampaignDomainEvent.sequence >= start,
        )
        .order_by(CampaignDomainEvent.sequence.asc())
    ).all()
    picked: list[Encounter] = []
    for encounter, event in rows:
        if end is not None:
            in_range = int(event.sequence) <= int(end)
        else:
            # No bound end cursor (derived finalization failed): fall back to
            # the completion timestamp so a later arc's fights never count.
            in_range = (
                adventure.completed_at is not None
                and encounter.ended_at is not None
                and encounter.ended_at <= adventure.completed_at
            )
        by_completing_turn = (
            completion_event is not None
            and str((event.provenance or {}).get("turn_event_id") or "") == completion_event
        )
        if in_range or by_completing_turn:
            picked.append(encounter)
    return picked


def _xp_hook(db: Session, encounter: Encounter) -> EncounterEndFollowup | None:
    return db.execute(
        select(EncounterEndFollowup).where(
            EncounterEndFollowup.encounter_id == encounter.id,
            EncounterEndFollowup.hook_type == XP_HOOK,
        )
    ).scalars().first()


def award_adventure_xp(db: Session, adventure: Adventure) -> dict:
    """Credit the adventure's defeated-foe XP to its PCs exactly once.

    Flush-only: the closing handler owns the transaction, so sheet updates,
    ledger rows and hook completions commit (or roll back) together.
    Returns the adventure's full award ledger summary.
    """
    from app.characters.service import latest_sheet
    from app.combat.service import list_participants

    totals: dict[uuid.UUID, int] = {}
    breakdown: dict[uuid.UUID, list[dict]] = {}
    credited: list[str] = []
    for encounter in adventure_encounters(db, adventure):
        hook = _xp_hook(db, encounter)
        if hook is not None and hook.status == "complete":
            continue  # already credited (this or an earlier closing run)
        fates = dict(encounter.end_participant_outcomes or {})
        participants = list_participants(db, encounter.id)
        pcs = [p for p in participants if p.kind == "pc" and p.character_id is not None]
        defeated = [
            p for p in participants
            if p.kind in ("npc", "monster") and fates.get(str(p.id)) in DEFEATED_FATES
        ]
        encounter_xp = sum(participant_xp(db, p) for p in defeated)
        share = encounter_xp // len(pcs) if pcs else 0
        for pc in pcs:
            totals[pc.character_id] = totals.get(pc.character_id, 0) + share
            breakdown.setdefault(pc.character_id, []).append({
                "encounter_id": str(encounter.id),
                "encounter_xp": encounter_xp,
                "pc_count": len(pcs),
                "share": share,
            })
        if hook is not None:
            hook.status = "complete"
            hook.error = None
            hook.attempts = int(hook.attempts or 0) + 1
            hook.result = {
                "adventure_id": str(adventure.id),
                "encounter_xp": encounter_xp,
                "pc_count": len(pcs),
                "share": share,
                "defeated_participant_ids": [str(p.id) for p in defeated],
            }
        credited.append(str(encounter.id))

    for character_id in sorted(totals, key=str):
        existing = db.execute(
            select(AdventureXpAward).where(
                AdventureXpAward.adventure_id == adventure.id,
                AdventureXpAward.character_id == character_id,
            )
        ).scalars().first()
        if existing is not None:
            continue
        amount = totals[character_id]
        sheet = latest_sheet(db, character_id)
        before = after = level = None
        if sheet is not None:
            before = int(sheet.experience_points or 0)
            after = before + amount
            sheet.experience_points = after
            level = level_for_xp(after)
        db.add(AdventureXpAward(
            adventure_id=adventure.id,
            campaign_id=adventure.campaign_id,
            character_id=character_id,
            xp_awarded=amount if sheet is not None else 0,
            xp_before=before,
            xp_after=after,
            qualifies_for_level=level,
            breakdown=breakdown.get(character_id, []),
        ))
        structured_log(
            logger, logging.INFO, "adventure_xp_awarded",
            adventure_id=str(adventure.id), character_id=str(character_id),
            xp_awarded=amount if sheet is not None else 0, xp_after=after,
            sheet_missing=sheet is None,
            level_up_available=bool(sheet is not None and level > int(sheet.level or 1)),
        )
    db.flush()

    rows = db.execute(
        select(AdventureXpAward)
        .where(AdventureXpAward.adventure_id == adventure.id)
        .order_by(AdventureXpAward.character_id.asc())
    ).scalars().all()
    return {
        "credited_encounter_ids": credited,
        "total_awarded": sum(int(r.xp_awarded or 0) for r in rows),
        "awards": [
            {
                "character_id": str(r.character_id),
                "xp_awarded": int(r.xp_awarded or 0),
                "xp_after": r.xp_after,
                "qualifies_for_level": r.qualifies_for_level,
            }
            for r in rows
        ],
    }
