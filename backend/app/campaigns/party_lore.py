"""Public party composition + private character lore — issue #244.

Composition is a derived, side-effect-free read projection over the existing
lobby members/sheets (name/race/class/level only — never secret lore). Lore
rows use #211 ``private`` semantics: the owning player reads/writes their own
row; anyone else (including the campaign owner) gets a fail-closed 404 with
no existence leak. The AI DM seed job consumes lore through
:func:`get_seed_lore_bundle` (DM-internal, never part of a public payload).
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.service import character_launch_validity
from models.campaigns import Campaign, CampaignCharacterLore, CampaignMember

logger = logging.getLogger(__name__)

#: Private lore content bounds (failure/recovery: bounded, idempotent writes).
LORE_MAX_LENGTH = 4000

#: Campaign statuses where private setup lore is writable. Reads survive
#: refresh/reconnect and the start transition; writes lock with the lobby
#: (mirrors selection/readiness ``is_launch_locked``).
LORE_WRITABLE_STATUSES = frozenset({"lobby"})


class LoreValidationError(ValueError):
    """Malformed lore payload (router maps to HTTP 422)."""


class LoreAuthorizationError(PermissionError):
    """Private lore access denied — router maps to HTTP 404 (fail closed)."""


class LoreStatusError(ValueError):
    """Lore write outside the pre-start window (router maps to HTTP 409)."""


def validate_lore_content(payload: object) -> str:
    if not isinstance(payload, dict):
        raise LoreValidationError("Request body must be an object")
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LoreValidationError("content must be a non-empty string")
    stripped = content.strip()
    if len(stripped) > LORE_MAX_LENGTH:
        raise LoreValidationError(f"content must be {LORE_MAX_LENGTH} characters or fewer")
    return stripped


def require_lore_writable(campaign: Campaign) -> None:
    if str(campaign.status or "").lower() not in LORE_WRITABLE_STATUSES:
        logger.warning(
            "character_lore write rejected campaign_id=%s status=%s reason=locked",
            campaign.id,
            campaign.status,
        )
        raise LoreStatusError("Private setup lore is locked after the campaign leaves the lobby")


def _public_sheet_fields(char, sheet) -> dict:
    char_class = (getattr(sheet, "char_class", None) or "").strip() if sheet is not None else ""
    classes = getattr(sheet, "classes", None) if sheet is not None else None
    class_names = (
        [str(c.get("class_name") or "").strip() for c in classes if isinstance(c, dict) and str(c.get("class_name") or "").strip()]
        if isinstance(classes, list)
        else []
    )
    if char_class and char_class not in class_names:
        class_names = [char_class, *class_names]
    return {
        "character_id": str(char.id),
        "character_name": char.name,
        "race": (getattr(sheet, "race", None) or "").strip() or None if sheet is not None else None,
        "classes": class_names,
        "level": getattr(sheet, "level", None) if sheet is not None else None,
    }


def build_party_composition(db: Session, members: list[CampaignMember]) -> dict:
    """Side-effect-free public composition over lobby members.

    Only explicitly public fields (name/race/classes/level + readiness).
    Never touches ``campaign_character_lore`` or sheet lore columns. A
    failure here must not fall back to private data — callers surface the
    error, never a secret-bearing payload.
    """
    from models.characters import Character, Dnd5eCharacterSheet

    entries: list[dict] = []
    for m in members:
        entry: dict = {
            "user_id": str(m.user_id),
            "is_ready": bool(m.is_ready),
            "character_id": None,
            "character_name": None,
            "race": None,
            "classes": [],
            "level": None,
        }
        char_id = getattr(m, "selected_character_id", None)
        if char_id is not None:
            char = db.get(Character, char_id)
            if char is not None:
                sheet = db.execute(
                    select(Dnd5eCharacterSheet)
                    .where(Dnd5eCharacterSheet.character_id == char.id)
                    .order_by(Dnd5eCharacterSheet.updated_at.desc())
                ).scalars().first()
                entry.update(_public_sheet_fields(char, sheet))
        entries.append(entry)

    class_counts: dict[str, int] = dict(Counter(c for e in entries for c in e["classes"]))
    ready_count = sum(1 for e in entries if e["is_ready"])
    return {
        "size": len(entries),
        "ready_count": ready_count,
        "class_counts": class_counts,
        "members": entries,
    }


def build_party_advice(composition: dict) -> dict:
    """Advisory gaps/overlap from public composition — never prescriptive.

    Pure function of the public projection (no lore access). Returns hints
    the character creator may surface; callers must not enforce them.
    """
    counts: dict[str, int] = dict(composition.get("class_counts") or {})
    size = int(composition.get("size") or 0)
    suggestions: list[str] = []
    if size == 0:
        suggestions.append("No party members yet — any class is a good first pick.")
        return {"suggestions": suggestions, "class_counts": counts, "enforced": False}
    # Broad role coverage heuristic (advisory only).
    known = {k.lower() for k in counts}
    has_martial = bool(known & {"fighter", "barbarian", "paladin", "ranger", "monk", "rogue"})
    has_caster = bool(known & {"wizard", "sorcerer", "warlock", "cleric", "druid", "bard"})
    has_support = bool(known & {"cleric", "bard", "druid", "paladin"})
    if not has_martial:
        suggestions.append("Party has no martial class yet — a Fighter or Rogue could round out the front line.")
    if not has_caster:
        suggestions.append("Party has no full caster yet — a Wizard or Cleric could add versatility.")
    if not has_support:
        suggestions.append("Party has no dedicated support yet — a Cleric or Bard could help.")
    for cls, n in sorted(counts.items(), key=lambda kv: -kv[1])[:3]:
        if n >= 3:
            suggestions.append(f"Party already has {n} {cls} characters — consider a different role for variety.")
    if not suggestions:
        suggestions.append("Party looks balanced — pick what excites you.")
    return {"suggestions": suggestions, "class_counts": counts, "enforced": False}


def get_own_lore(
    db: Session, *, campaign_id: uuid_lib.UUID, character_id: uuid_lib.UUID, user_id: uuid_lib.UUID
) -> CampaignCharacterLore:
    """Return the caller's own lore row or raise fail-closed 404."""
    row = db.execute(
        select(CampaignCharacterLore).where(
            CampaignCharacterLore.campaign_id == campaign_id,
            CampaignCharacterLore.character_id == character_id,
        )
    ).scalars().first()
    if row is None or row.user_id != user_id:
        # No existence leak: missing vs unauthorized are indistinguishable.
        raise LoreAuthorizationError("Character lore not found")
    return row


def list_lore_presence(db: Session, *, campaign_id: uuid_lib.UUID) -> dict[str, bool]:
    """Presence map (character_id -> has lore) without content.

    Used for DM/seed observability counts; content stays out of projections.
    """
    rows = db.execute(
        select(CampaignCharacterLore.character_id).where(
            CampaignCharacterLore.campaign_id == campaign_id
        )
    ).all()
    return {str(r[0]): True for r in rows}


def get_seed_lore_bundle(db: Session, *, campaign_id: uuid_lib.UUID) -> list[dict]:
    """DM-internal seed input — issue #245 seam.

    Returns private lore rows for the world-seed job. This is the ONLY path
    that reads other players' content server-side; it must never be attached
    to a public seed/narration payload — #245 must copy content into
    restricted seed inputs only. No logging of raw content here.
    """
    rows = db.execute(
        select(CampaignCharacterLore).where(
            CampaignCharacterLore.campaign_id == campaign_id
        )
    ).scalars().all()
    return [
        {
            "character_id": str(r.character_id),
            "user_id": str(r.user_id),
            "content": r.content,
            "version": r.version,
        }
        for r in rows
    ]
