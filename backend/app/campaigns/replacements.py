"""Replacement-character lifecycle — issue #266.

Canonical rules (single code path, no legacy shims):

- A campaign PC's lifecycle is ``active`` (launch PCs backfill on first
  touch), ``dead``, or ``retired``. Terminal states are never deleted: the
  dead PC's sheet, inventory, and world relationships stay intact as
  historical canon, and deleting/editing a canon PC through the character
  API is rejected.
- A member may activate a replacement only when a valid replacement
  condition exists: their currently selected PC is ``dead``/``retired``, as
  recorded in this campaign. A full-party wipe (every selected PC ``dead``)
  is additionally flagged as a TPK for observability; each member still
  replaces their own fallen PC.
- Exactly one active human-controlled PC per member: the transition swaps
  ``selected_character_id`` atomically inside the caller's revision-guarded
  transaction, so a failed transition leaves the prior lifecycle state
  intact and retryable.
- Nothing is copied from the dead PC to the replacement — no sheet data,
  no inventory, no private knowledge. The replacement starts fresh and the
  AI DM introduces them through normal forward-DM play (the
  ``campaign.pc_replaced`` domain event surfaces in RECENT_HISTORY);
  ``introduction_status`` tracks pending → introduced explicitly.

All writers below are flush-only: the caller (router) owns the transaction
via ``commit_campaign_mutation`` + the HTTP idempotency guard.
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.observability.tracing import structured_log
from models.campaigns import Campaign, CampaignMember, CampaignPcLifecycle

logger = logging.getLogger(__name__)

PC_LIFECYCLE_STATUSES = frozenset({"active", "dead", "retired"})
TERMINAL_PC_STATUSES = frozenset({"dead", "retired"})
INTRODUCTION_STATUSES = frozenset({"na", "pending_introduction", "introduced"})


class PcLifecycleError(Exception):
    """Domain failure with its canonical HTTP mapping."""

    def __init__(self, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def get_lifecycle(
    db: Session, campaign_id: uuid_lib.UUID, character_id: uuid_lib.UUID
) -> CampaignPcLifecycle | None:
    return db.get(CampaignPcLifecycle, {"campaign_id": campaign_id, "character_id": character_id})


def ensure_active_lifecycle(
    db: Session,
    campaign_id: uuid_lib.UUID,
    character_id: uuid_lib.UUID,
    user_id: uuid_lib.UUID,
) -> tuple[CampaignPcLifecycle, bool]:
    """Backfill a launch PC (selected before #266) as ``active``.

    Returns (row, created). Never touches an existing row.
    """
    existing = get_lifecycle(db, campaign_id, character_id)
    if existing is not None:
        return existing, False
    row = CampaignPcLifecycle(
        campaign_id=campaign_id,
        character_id=character_id,
        user_id=user_id,
        status="active",
        introduction_status="na",
    )
    db.add(row)
    db.flush()
    return row, True


def lifecycle_status(
    db: Session, campaign_id: uuid_lib.UUID, character_id: uuid_lib.UUID | None
) -> str | None:
    """Current lifecycle status, or None when the PC has no row / no PC."""
    if character_id is None:
        return None
    row = get_lifecycle(db, campaign_id, character_id)
    return row.status if row is not None else None


def campaign_is_tpk(db: Session, campaign_id: uuid_lib.UUID) -> bool:
    """True when every selected party PC is ``dead`` (missing row == active).

    A wipe where members retired voluntarily is not a TPK.
    """
    members = db.execute(
        select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
    ).scalars().all()
    selected = [m for m in members if m.selected_character_id is not None]
    if not selected:
        return False
    for m in selected:
        row = get_lifecycle(db, campaign_id, m.selected_character_id)
        status = row.status if row is not None else "active"
        if status != "dead":
            return False
    return True


def replacement_eligibility(
    db: Session, campaign: Campaign, member: CampaignMember
) -> dict:
    """Whether this member may activate a replacement PC right now."""
    dead_character_id: uuid_lib.UUID | None = None
    reason: str | None = None
    current_id = member.selected_character_id
    if current_id is not None:
        row = get_lifecycle(db, campaign.id, current_id)
        status = row.status if row is not None else "active"
        if status in TERMINAL_PC_STATUSES:
            dead_character_id = current_id
    is_tpk = campaign_is_tpk(db, campaign.id)
    if dead_character_id is None:
        reason = "Replacement requires a dead or retired PC; the current PC is still active"
    return {
        "eligible": dead_character_id is not None,
        "reason": reason,
        "dead_character_id": str(dead_character_id) if dead_character_id else None,
        "is_tpk": is_tpk,
    }


def declare_pc_death(
    db: Session,
    campaign: Campaign,
    character_id: uuid_lib.UUID,
    *,
    status: str = "dead",
    cause: str | None = None,
    is_tpk: bool = False,
    actor_id: uuid_lib.UUID | None = None,
) -> CampaignPcLifecycle:
    """Record a party PC as ``dead`` (or ``retired``). Flush-only.

    The PC must be actively selected by a member of this campaign; the row,
    the Character, and its sheet are otherwise untouched (canon preserved).
    """
    target = str(status or "dead").strip().lower()
    if target not in TERMINAL_PC_STATUSES:
        raise PcLifecycleError("status must be dead or retired", status_code=400)
    holder = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign.id,
            CampaignMember.selected_character_id == character_id,
        )
    ).scalars().first()
    if holder is None:
        structured_log(
            logger, logging.WARNING, "pc_death_rejected",
            campaign_id=str(campaign.id), character_id=str(character_id),
            reason="not_active_party_pc",
        )
        raise PcLifecycleError(
            "Only an actively selected party PC can be declared dead or retired",
            status_code=409,
        )
    row, _ = ensure_active_lifecycle(db, campaign.id, character_id, holder.user_id)
    if row.status in TERMINAL_PC_STATUSES:
        structured_log(
            logger, logging.WARNING, "pc_death_rejected",
            campaign_id=str(campaign.id), character_id=str(character_id),
            reason="already_terminal", status=row.status,
        )
        raise PcLifecycleError(
            f"PC is already {row.status}; duplicate death declarations are rejected",
            status_code=409,
        )
    row.status = target
    row.cause = (cause or "").strip() or None
    row.died_at = datetime.now(timezone.utc)
    row.is_tpk = bool(is_tpk)
    db.flush()
    structured_log(
        logger, logging.INFO, "pc_death_declared",
        campaign_id=str(campaign.id), character_id=str(character_id),
        user_id=str(holder.user_id), status=target, is_tpk=bool(is_tpk),
        actor_id=str(actor_id) if actor_id else None,
    )
    return row


def activate_replacement(
    db: Session,
    campaign: Campaign,
    member: CampaignMember,
    new_character,
    *,
    actor_id: uuid_lib.UUID | None = None,
) -> dict:
    """Swap the member's fallen PC for a fresh replacement. Flush-only.

    Validates everything before mutating, so any failure leaves the prior
    active/dead lifecycle state intact and retryable. Copies nothing from
    the dead PC: the replacement starts fresh with no inherited knowledge.
    """
    from app.campaigns.service import character_launch_validity
    from models.characters import Character, Dnd5eCharacterSheet

    if str(getattr(campaign, "status", "lobby")) == "lobby":
        raise PcLifecycleError(
            "Replacements are post-launch only; use lobby character selection before start",
            status_code=409,
        )
    eligibility = replacement_eligibility(db, campaign, member)
    if not eligibility["eligible"]:
        structured_log(
            logger, logging.WARNING, "pc_replacement_rejected",
            campaign_id=str(campaign.id), user_id=str(member.user_id),
            new_character_id=str(getattr(new_character, "id", "?")),
            reason="no_replacement_condition",
        )
        raise PcLifecycleError(eligibility["reason"] or "No valid replacement condition", status_code=409)
    dead_id = uuid_lib.UUID(eligibility["dead_character_id"])

    if new_character.owner_id != member.user_id:
        structured_log(
            logger, logging.WARNING, "pc_replacement_rejected",
            campaign_id=str(campaign.id), user_id=str(member.user_id),
            new_character_id=str(new_character.id), reason="not_owner",
        )
        raise PcLifecycleError("Only your own character can be activated as a replacement", status_code=403)
    if new_character.id == dead_id:
        raise PcLifecycleError("The fallen PC cannot replace itself", status_code=409)
    new_row = get_lifecycle(db, campaign.id, new_character.id)
    if new_row is not None and new_row.status in TERMINAL_PC_STATUSES:
        raise PcLifecycleError("A dead or retired PC cannot return as a replacement", status_code=409)
    other_holder = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign.id,
            CampaignMember.selected_character_id == new_character.id,
        )
    ).scalars().first()
    if other_holder is not None:
        raise PcLifecycleError("That character is already the active PC of a party member", status_code=409)
    # One active PC per member: the member must still point at the fallen PC.
    # If they already transitioned (e.g. duplicate retry raced ahead), reject.
    if member.selected_character_id != dead_id:
        structured_log(
            logger, logging.WARNING, "pc_replacement_rejected",
            campaign_id=str(campaign.id), user_id=str(member.user_id),
            new_character_id=str(new_character.id), reason="duplicate_transition",
        )
        raise PcLifecycleError(
            "A replacement was already activated; one active PC per member",
            status_code=409,
        )
    sheet = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == new_character.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()
    validity = character_launch_validity(new_character, sheet)
    if not validity["is_valid"]:
        structured_log(
            logger, logging.WARNING, "pc_replacement_rejected",
            campaign_id=str(campaign.id), user_id=str(member.user_id),
            new_character_id=str(new_character.id), reason="incomplete_character",
            missing=",".join(validity["missing"]),
        )
        raise PcLifecycleError(
            f"Replacement character incomplete: missing {', '.join(validity['missing'])}",
            status_code=422,
        )

    # ── Mutate (all-or-nothing in the caller's transaction) ──────────────
    dead_row = get_lifecycle(db, campaign.id, dead_id)
    assert dead_row is not None and dead_row.status in TERMINAL_PC_STATUSES
    if dead_row.replaced_by_character_id is not None:
        raise PcLifecycleError(
            "This fallen PC already has a replacement; duplicate transitions are rejected",
            status_code=409,
        )
    # A replacement stems from a TPK when the party is currently wiped OR the
    # fallen PC itself fell in a declared wipe (earlier replacements may have
    # already revived part of the party).
    is_tpk = bool(eligibility["is_tpk"] or dead_row.is_tpk)
    dead_row.replaced_by_character_id = new_character.id
    replacement_row = CampaignPcLifecycle(
        campaign_id=campaign.id,
        character_id=new_character.id,
        user_id=member.user_id,
        status="active",
        replacement_of_character_id=dead_id,
        introduction_status="pending_introduction",
    )
    db.add(replacement_row)
    member.selected_character_id = new_character.id
    db.flush()
    structured_log(
        logger, logging.INFO, "pc_replacement_activated",
        campaign_id=str(campaign.id), user_id=str(member.user_id),
        dead_character_id=str(dead_id), new_character_id=str(new_character.id),
        is_tpk=is_tpk,
        actor_id=str(actor_id) if actor_id else None,
    )
    return {
        "dead_character_id": str(dead_id),
        "new_character_id": str(new_character.id),
        "is_tpk": is_tpk,
    }


def mark_replacement_introduced(
    db: Session,
    campaign: Campaign,
    character_id: uuid_lib.UUID,
    *,
    actor_id: uuid_lib.UUID | None = None,
) -> CampaignPcLifecycle:
    """Complete the narrative introduction of a replacement PC. Flush-only."""
    row = get_lifecycle(db, campaign.id, character_id)
    if row is None or row.status != "active":
        raise PcLifecycleError("Only an active replacement PC can be introduced", status_code=409)
    if row.replacement_of_character_id is None:
        raise PcLifecycleError("Launch PCs need no introduction; only replacements do", status_code=409)
    if row.introduction_status == "introduced":
        raise PcLifecycleError("Replacement was already introduced", status_code=409)
    if row.introduction_status != "pending_introduction":
        raise PcLifecycleError("Replacement introduction is not pending", status_code=409)
    row.introduction_status = "introduced"
    row.introduced_at = datetime.now(timezone.utc)
    db.flush()
    structured_log(
        logger, logging.INFO, "pc_introduction_completed",
        campaign_id=str(campaign.id), character_id=str(character_id),
        user_id=str(row.user_id),
        actor_id=str(actor_id) if actor_id else None,
    )
    return row


def party_roster(db: Session, campaign: Campaign) -> dict:
    """Public-safe party projection: active roster + preserved history.

    Never includes secret lore (backstory/notes/etc.) — same boundary as the
    lobby projection.
    """
    from models.characters import Character, Dnd5eCharacterSheet

    members = db.execute(
        select(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
    ).scalars().all()
    lifecycles = {
        row.character_id: row
        for row in db.execute(
            select(CampaignPcLifecycle).where(CampaignPcLifecycle.campaign_id == campaign.id)
        ).scalars().all()
    }
    active: list[dict] = []
    historical: list[dict] = []
    pending_introductions: list[dict] = []
    seen_historical: set = set()
    for character_id, row in lifecycles.items():
        if row.status in TERMINAL_PC_STATUSES:
            # Fallen PCs are historical canon even while still selected —
            # the member points at them only until a replacement activates.
            char = db.get(Character, character_id)
            if char is None:  # pragma: no cover — FK CASCADE normally prevents this
                continue
            historical.append(_canon_entry(db, row, char))
            seen_historical.add(character_id)
    for m in members:
        if m.selected_character_id is None:
            continue
        if m.selected_character_id in seen_historical:
            continue
        char = db.get(Character, m.selected_character_id)
        if char is None:
            continue
        row = lifecycles.get(m.selected_character_id)
        sheet = db.execute(
            select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == char.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        entry = {
            "character_id": str(char.id),
            "user_id": str(m.user_id),
            "name": char.name,
            "race": sheet.race if sheet else None,
            "char_class": sheet.char_class if sheet else None,
            "level": sheet.level if sheet else None,
            "lifecycle_status": row.status if row else "active",
            "introduction_status": row.introduction_status if row else "na",
            "replacement_of_character_id": (
                str(row.replacement_of_character_id) if row and row.replacement_of_character_id else None
            ),
        }
        active.append(entry)
        if row is not None and row.introduction_status == "pending_introduction":
            pending_introductions.append(entry)
    return {
        "campaign_id": str(campaign.id),
        "campaign_status": campaign.status,
        "is_tpk": campaign_is_tpk(db, campaign.id),
        "active": sorted(active, key=lambda e: e["name"]),
        "historical": sorted(historical, key=lambda e: e["name"]),
        "pending_introductions": pending_introductions,
    }


def _canon_entry(db: Session, row: CampaignPcLifecycle, char) -> dict:
    from models.characters import Dnd5eCharacterSheet

    sheet = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == char.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()
    return {
        "character_id": str(char.id),
        "user_id": str(row.user_id),
        "name": char.name,
        "race": sheet.race if sheet else None,
        "char_class": sheet.char_class if sheet else None,
        "level": sheet.level if sheet else None,
        "lifecycle_status": row.status,
        "cause": row.cause,
        "died_at": row.died_at.isoformat() if row.died_at else None,
        "is_tpk": bool(row.is_tpk),
        "replaced_by_character_id": (
            str(row.replaced_by_character_id) if row.replaced_by_character_id else None
        ),
    }


def is_historical_canon(db: Session, character_id: uuid_lib.UUID) -> bool:
    """True when the character is preserved canon in any campaign.

    Canon PCs (terminal lifecycle rows, or PCs referenced by a replacement
    link) must never be edited or deleted through the character API.
    """
    terminal = db.execute(
        select(CampaignPcLifecycle).where(
            CampaignPcLifecycle.character_id == character_id,
            CampaignPcLifecycle.status.in_(("dead", "retired")),
        )
    ).scalars().first()
    if terminal is not None:
        return True
    linked = db.execute(
        select(CampaignPcLifecycle).where(
            (CampaignPcLifecycle.replaced_by_character_id == character_id)
            | (CampaignPcLifecycle.replacement_of_character_id == character_id)
        )
    ).scalars().first()
    return linked is not None
