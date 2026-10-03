"""Party composition/advice (#244) and PC death/replacement lifecycle (#266)."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns import replacements
from app.campaigns.members import campaign_members
from app.campaigns.party_lore import build_party_advice, build_party_composition
from app.campaigns.service import parse_character_id
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, parse_uuid_or_404, require_expected_revision, run_campaign_command
from app.deps.idempotency import command_keys
from database import get_db
from models.campaigns import Campaign
from models.characters import Character

router = APIRouter()


@router.get("/api/campaigns/{campaign_id}/party-composition")
def get_party_composition(
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    """Public party composition — members only, side-effect-free."""
    return {"party_composition": build_party_composition(db, campaign_members(db, campaign.id))}


@router.get("/api/campaigns/{campaign_id}/party-advice")
def get_party_advice(
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    """Advisory party gaps/overlap for the character creator.

    Pure function of the public composition. Advisory only: the response
    carries ``enforced: False`` and nothing rejects a character choice on it.
    """
    composition = build_party_composition(db, campaign_members(db, campaign.id))
    return {"advice": build_party_advice(composition)}


@router.get("/api/campaigns/{campaign_id}/party")
def get_party_roster(
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    """Active roster + preserved fallen-PC canon + pending introductions."""
    return {"party": replacements.party_roster(db, campaign)}


@router.post("/api/campaigns/{campaign_id}/pc-deaths")
def declare_pc_death(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("owner", forbidden="Only the owner can declare PC death")),
    db: Session = Depends(get_db),
):
    """Declare a party PC dead/retired — terminal canon state, never deletion."""
    char_id = parse_character_id(payload.get("character_id"))
    target_status = str(payload.get("status") or "dead").strip().lower()
    if target_status not in ("dead", "retired"):
        raise HTTPException(status_code=400, detail="status must be dead or retired")
    cause = payload.get("cause")
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.pc.death", scope_type="campaign", scope_id=campaign.id,
        payload={**payload, "character_id": str(char_id)},
        execute=lambda: replacements.commit_pc_death(
            db, campaign.id, actor_id=profile.id, character_id=char_id,
            status=target_status,
            cause=str(cause) if cause is not None else None,
            is_tpk=bool(payload.get("is_tpk", False)),
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.post("/api/campaigns/{campaign_id}/pc-replacements")
def activate_pc_replacement(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("member")),
    db: Session = Depends(get_db),
):
    """Activate a replacement PC for the caller's fallen PC."""
    char_id = parse_character_id(payload.get("character_id"))
    new_char = db.get(Character, char_id)
    if new_char is None or new_char.is_deleted:
        raise HTTPException(status_code=404, detail="Character not found")
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.pc.replacement",
        scope_type="campaign_member", scope_id=f"{campaign.id}:{profile.id}",
        payload={**payload, "character_id": str(char_id)},
        execute=lambda: replacements.commit_replacement(
            db, campaign.id, user_id=profile.id, character_id=char_id,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.post("/api/campaigns/{campaign_id}/pc-replacements/{character_id}/introduce")
def introduce_pc_replacement(
    character_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for(
        "owner", forbidden="Only the owner can mark introductions", invalid="Invalid campaign or character id",
    )),
    db: Session = Depends(get_db),
):
    """Mark a replacement PC narratively introduced — normal-play hook."""
    char_id = parse_uuid_or_404(character_id, "Invalid campaign or character id")
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.pc.introduction", scope_type="campaign", scope_id=campaign.id,
        payload={**payload, "character_id": str(char_id)},
        execute=lambda: replacements.commit_introduction(
            db, campaign.id, actor_id=profile.id, character_id=char_id,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )
