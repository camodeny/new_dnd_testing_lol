"""Campaign membership: lobby projections, launch PC selection, readiness,
removal, and the public launch roster (issues #240/#241/#244).

Launch selection/readiness are lobby-only and revision-guarded: each command
re-verifies ownership and lobby status on the locked campaign row so a
failed command can never leave a member pointing at an unauthorized or
incomplete character.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.campaigns.events import commit_campaign_mutation
from app.campaigns.service import (
    CampaignCommandError,
    character_launch_validity,
    compute_start_eligibility,
    is_launch_locked,
    parse_character_id,
)
from app.characters.service import latest_sheet
from app.threads.service import get_lobby_thread
from models.campaigns import Campaign, CampaignInvite, CampaignMember
from models.characters import Character
from models.profiles import Profile
from models.threads import CampaignThread, CampaignThreadMember

logger = logging.getLogger(__name__)


def campaign_members(db: Session, campaign_id: uuid.UUID) -> list[CampaignMember]:
    return list(db.execute(
        select(CampaignMember).where(CampaignMember.campaign_id == campaign_id)
    ).scalars().all())


def member_lobby_projection(db: Session, m: CampaignMember) -> dict:
    """Public member projection — never secret lore (backstory/notes/etc)."""
    prof = db.get(Profile, m.user_id)
    projection = {
        "user_id": str(m.user_id),
        "username": prof.username if prof else "adventurer",
        "email": prof.email if prof else None,
        "role": m.role,
        "selected_character_id": str(m.selected_character_id) if m.selected_character_id else None,
        "character_id": str(m.selected_character_id) if m.selected_character_id else None,
        "character_name": None,
        "is_ready": bool(m.is_ready),
        "ready_at": m.ready_at.isoformat() if getattr(m, "ready_at", None) else None,
        "character_valid": False,
        "character_progress": {"completed": 0, "total": 3, "percent": 0},
        "character_missing": [],
    }
    if m.selected_character_id:
        char = db.get(Character, m.selected_character_id)
        if char is not None and not char.is_deleted:
            sheet = latest_sheet(db, char.id)
            validity = character_launch_validity(char, sheet)
            projection.update({
                "character_name": char.name,
                "character_valid": validity["is_valid"],
                "character_progress": validity["progress"],
                "character_missing": validity["missing"],
                "character_race": sheet.race if sheet else None,
                "character_class": sheet.char_class if sheet and sheet.char_class else None,
                "character_classes": sheet.classes if sheet and sheet.classes else None,
                "character_level": sheet.level if sheet else None,
            })
        else:
            projection["character_missing"] = ["missing"]
    return projection


def lobby_projection(db: Session, campaign: Campaign, viewer_id: uuid.UUID) -> dict:
    """Authoritative lobby projection — issue #241. Side-effect-free."""
    from app.campaigns.invites import invite_usability, lobby_invite_projection
    from app.campaigns.party_lore import build_party_composition
    members = campaign_members(db, campaign.id)
    viewer_is_owner = campaign.owner_id == viewer_id
    invite_rows = db.execute(
        select(CampaignInvite)
        .where(CampaignInvite.campaign_id == campaign.id)
        .order_by(CampaignInvite.created_at.asc())
    ).scalars().all()
    # Read-only lobby chat discovery (#243): no creation here; the full chat
    # snapshot lives behind GET .../lobby/chat.
    lobby_thread = get_lobby_thread(db, campaign.id)
    # Public party composition (#244) — never includes private lore content.
    # A projection failure must fail with the error, never a secret-bearing
    # fallback payload.
    try:
        party = build_party_composition(db, members)
    except Exception as exc:
        logger.warning("party_composition failed campaign_id=%s error=%s", campaign.id, exc)
        raise CampaignCommandError(status_code=500, detail="Party composition unavailable") from exc
    return {
        "campaign": campaign.to_dict(),
        "members": [member_lobby_projection(db, m) for m in members],
        "eligibility": compute_start_eligibility(campaign, members, db),
        "launch_locked": is_launch_locked(campaign.status),
        "lobby_thread_id": str(lobby_thread.id) if lobby_thread else None,
        "party_composition": party,
        # Joined vs outstanding invited state (#242). Revoked/expired history
        # stays owner-only; members receive only currently usable rows (still
        # without bearer codes or raw emails — see lobby_invite_projection).
        "invites": [
            lobby_invite_projection(inv, viewer_is_owner=viewer_is_owner)
            for inv in invite_rows
            if viewer_is_owner or invite_usability(inv)[0]
        ],
        "outstanding_invites": sum(1 for inv in invite_rows if invite_usability(inv)[0]),
    }


def launch_roster(db: Session, campaign_id: uuid.UUID) -> list[dict]:
    """Public launch roster — only approved public character info."""
    members = db.execute(
        select(CampaignMember).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.selected_character_id.is_not(None),
        )
    ).scalars().all()
    roster = []
    for m in members:
        char = db.get(Character, m.selected_character_id)
        if char is None or char.is_deleted:
            continue
        sheet = latest_sheet(db, char.id)
        roster.append({
            "character_id": str(char.id),
            "user_id": str(m.user_id),
            "name": char.name,
            "race": sheet.race if sheet else None,
            "char_class": sheet.char_class if sheet else None,
            "classes": sheet.classes if sheet else None,
            "level": sheet.level if sheet else None,
            "is_ready": bool(m.is_ready),
        })
    return roster


def _require_member(db: Session, campaign_id: uuid.UUID, user_id: uuid.UUID) -> CampaignMember:
    member = db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": user_id})
    if member is None:
        raise CampaignCommandError(status_code=403, detail="Not a member of this campaign")
    return member


def _require_selectable(char: Character | None, user_id: uuid.UUID) -> Character:
    if char is None or char.is_deleted:
        raise CampaignCommandError(status_code=404, detail="Character not found")
    if char.owner_id != user_id:
        raise CampaignCommandError(status_code=403, detail="Only your own character can be selected")
    if char.status != "complete":
        raise CampaignCommandError(status_code=409, detail="Finish the character draft before selecting it")
    return char


def selectable_character(db: Session, campaign: Campaign, user_id: uuid.UUID, raw_character_id) -> Character:
    """Resolve the requested launch PC, verifying ownership and completion."""
    char_id = parse_character_id(raw_character_id)
    char = db.get(Character, char_id)
    if char is not None and not char.is_deleted and char.owner_id != user_id:
        logger.warning(
            "character selection rejected campaign_id=%s actor_id=%s character_id=%s reason=not_owner",
            campaign.id, user_id, char_id,
        )
    return _require_selectable(char, user_id)


def _unchanged(db: Session, campaign: Campaign, member: CampaignMember) -> dict:
    return {
        "ok": True,
        "campaign": campaign.to_dict(),
        "member": member_lobby_projection(db, member),
        "idempotent": True,
    }


def _member_result(db: Session, campaign: Campaign, user_id: uuid.UUID, event) -> dict:
    refreshed = db.get(CampaignMember, {"campaign_id": campaign.id, "user_id": user_id})
    return {
        "ok": True,
        "campaign": campaign.to_dict(),
        "member": member_lobby_projection(db, refreshed),
        "event": event.to_dict(),
    }


def unchanged_selection(db: Session, campaign: Campaign, user_id: uuid.UUID, character_id: uuid.UUID) -> dict | None:
    """409 once launch-locked; the no-op result when already selected."""
    if is_launch_locked(campaign.status):
        raise CampaignCommandError(status_code=409, detail="Launch character is locked after campaign start")
    member = _require_member(db, campaign.id, user_id)
    return _unchanged(db, campaign, member) if member.selected_character_id == character_id else None


def select_character(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    character_id: uuid.UUID,
    expected_revision: int,
    operation_id: str,
) -> dict:
    def _mutate(locked: Campaign):
        if is_launch_locked(locked.status):
            logger.warning(
                "character selection rejected campaign_id=%s actor_id=%s status=%s reason=locked",
                campaign_id, user_id, locked.status,
            )
            raise CampaignCommandError(status_code=409, detail="Launch character is locked after campaign start")
        current = _require_member(db, campaign_id, user_id)
        fresh = _require_selectable(db.get(Character, character_id), user_id)
        current.selected_character_id = fresh.id
        # Selection change requires re-ready.
        current.is_ready = False
        current.ready_at = None

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="campaign.member_character_selected",
        operation_id=operation_id,
        actor_id=user_id,
        targets={"user_id": str(user_id), "character_id": str(character_id)},
        payload={"user_id": str(user_id), "character_id": str(character_id)},
        mutate=_mutate,
        commit=False,
    )
    logger.info(
        "character selected campaign_id=%s actor_id=%s character_id=%s revision=%s",
        campaign_id, user_id, character_id, campaign_after.revision,
    )
    return _member_result(db, campaign_after, user_id, event)


def parse_ready(payload: dict) -> bool:
    if "ready" not in payload:
        raise CampaignCommandError(status_code=400, detail="ready is required")
    ready = payload["ready"]
    if not isinstance(ready, bool):
        raise CampaignCommandError(status_code=400, detail="ready must be a boolean")
    return ready


def _require_ready_character(db: Session, member: CampaignMember, user_id: uuid.UUID) -> None:
    char_id = member.selected_character_id
    if char_id is None:
        raise CampaignCommandError(status_code=422, detail="Select a character before marking ready")
    char = db.get(Character, char_id)
    if char is None or char.is_deleted or char.owner_id != user_id:
        raise CampaignCommandError(status_code=422, detail="Selected character is missing or not owned")
    validity = character_launch_validity(char, latest_sheet(db, char.id))
    if not validity["is_valid"]:
        logger.warning(
            "readiness rejected campaign_id=%s actor_id=%s character_id=%s missing=%s",
            member.campaign_id, user_id, char_id, validity["missing"],
        )
        raise CampaignCommandError(status_code=422, detail={
            "message": f"Character incomplete: missing {', '.join(validity['missing'])}",
            "missing": validity["missing"],
        })


def unchanged_readiness(db: Session, campaign: Campaign, user_id: uuid.UUID, ready: bool) -> dict | None:
    """409 once launch-locked; the no-op result when already in ``ready``;
    otherwise validates that marking ready is allowed (422)."""
    if is_launch_locked(campaign.status):
        raise CampaignCommandError(status_code=409, detail="Readiness is locked after campaign start")
    member = _require_member(db, campaign.id, user_id)
    if bool(member.is_ready) == ready:
        return _unchanged(db, campaign, member)
    if ready:
        _require_ready_character(db, member, user_id)
    return None


def set_readiness(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    ready: bool,
    expected_revision: int,
    operation_id: str,
) -> dict:
    def _mutate(locked: Campaign):
        if is_launch_locked(locked.status):
            logger.warning(
                "readiness rejected campaign_id=%s actor_id=%s status=%s reason=locked",
                campaign_id, user_id, locked.status,
            )
            raise CampaignCommandError(status_code=409, detail="Readiness is locked after campaign start")
        current = _require_member(db, campaign_id, user_id)
        if ready:
            if current.selected_character_id is None:
                raise CampaignCommandError(status_code=422, detail="Selected character is missing or not owned")
            _require_ready_character(db, current, user_id)
            current.is_ready = True
            current.ready_at = datetime.now(timezone.utc)
        else:
            current.is_ready = False
            current.ready_at = None

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="campaign.member_ready" if ready else "campaign.member_unready",
        operation_id=operation_id,
        actor_id=user_id,
        targets={"user_id": str(user_id)},
        payload={"user_id": str(user_id), "ready": ready},
        mutate=_mutate,
        commit=False,
    )
    logger.info(
        "readiness transition campaign_id=%s actor_id=%s ready=%s revision=%s",
        campaign_id, user_id, ready, campaign_after.revision,
    )
    return _member_result(db, campaign_after, user_id, event)


def remove_member(
    db: Session,
    campaign_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    target_id: uuid.UUID,
    expected_revision: int,
    operation_id: str,
) -> dict:
    """Owner removes a member (lobby-only), dropping their thread memberships."""

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            logger.warning(
                "campaign membership removal rejected campaign_id=%s actor_id=%s target_id=%s status=%s",
                campaign_id, actor_id, target_id, locked.status,
            )
            raise CampaignCommandError(status_code=409, detail="Campaign membership is locked after the lobby")
        member = db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": target_id})
        if member is None:
            raise CampaignCommandError(status_code=404, detail="Campaign member not found")
        db.delete(member)
        campaign_thread_ids = select(CampaignThread.id).where(CampaignThread.campaign_id == campaign_id)
        db.execute(
            delete(CampaignThreadMember).where(
                CampaignThreadMember.user_id == target_id,
                CampaignThreadMember.thread_id.in_(campaign_thread_ids),
            )
        )

    campaign_after, event = commit_campaign_mutation(
        db,
        campaign_id,
        expected_revision,
        event_type="campaign.member_removed",
        operation_id=operation_id,
        actor_id=actor_id,
        targets={"user_id": str(target_id)},
        payload={"user_id": str(target_id)},
        mutate=_mutate,
        commit=False,
    )
    logger.info(
        "campaign member removed campaign_id=%s actor_id=%s target_id=%s revision=%s",
        campaign_id, actor_id, target_id, campaign_after.revision,
    )
    return {"ok": True, "campaign": campaign_after.to_dict(), "event": event.to_dict()}
