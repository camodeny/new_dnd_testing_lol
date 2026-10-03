"""Membership, launch character selection, readiness, and the launch roster."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns import members
from app.deps.auth import current_profile
from app.deps.campaign import campaign_for, parse_uuid_or_404, require_expected_revision, run_campaign_command
from app.deps.idempotency import command_keys
from database import get_db
from models.campaigns import Campaign

router = APIRouter()


@router.get("/api/campaigns/{campaign_id}/members")
def list_campaign_members(
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    return {"members": [members.member_lobby_projection(db, m) for m in members.campaign_members(db, campaign.id)]}


@router.put("/api/campaigns/{campaign_id}/members/me/character")
def select_own_character(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("member", lock=True)),
    db: Session = Depends(get_db),
):
    """Select one owned PC — idempotent, ownership-verified, lobby-only."""
    char = members.selectable_character(db, campaign, profile.id, payload.get("character_id"))
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    unchanged = members.unchanged_selection(db, campaign, profile.id, char.id)
    if unchanged is not None:
        return unchanged
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.member.character.select",
        scope_type="campaign_member", scope_id=f"{campaign.id}:{profile.id}",
        payload={**payload, "character_id": str(char.id)},
        execute=lambda: members.select_character(
            db, campaign.id, user_id=profile.id, character_id=char.id,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.put("/api/campaigns/{campaign_id}/members/me/readiness")
def set_own_readiness(
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for("member", lock=True)),
    db: Session = Depends(get_db),
):
    """Ready/unready — reversible before start, lobby-only, validity-gated."""
    ready = members.parse_ready(payload)
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    unchanged = members.unchanged_readiness(db, campaign, profile.id, ready)
    if unchanged is not None:
        return unchanged
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.member.readiness",
        scope_type="campaign_member", scope_id=f"{campaign.id}:{profile.id}",
        payload=payload,
        execute=lambda: members.set_readiness(
            db, campaign.id, user_id=profile.id, ready=ready,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.delete("/api/campaigns/{campaign_id}/members/{user_id}")
def remove_campaign_member(
    user_id: str,
    payload: dict,
    request: Request,
    response: Response,
    profile=Depends(current_profile),
    campaign: Campaign = Depends(campaign_for(
        "owner", forbidden="Only owner can remove campaign members", invalid="Invalid campaign or user id",
    )),
    db: Session = Depends(get_db),
):
    target_id = parse_uuid_or_404(user_id, "Invalid campaign or user id")
    if target_id == campaign.owner_id:
        raise HTTPException(status_code=400, detail="Campaign owner cannot be removed")
    expected_revision = require_expected_revision(payload)
    operation_id, idempotency_key = command_keys(request, payload)
    return run_campaign_command(
        db, response, actor_id=profile.id, idempotency_key=idempotency_key,
        command_type="campaign.member.remove", scope_type="campaign", scope_id=campaign.id,
        payload={**payload, "user_id": str(target_id)},
        execute=lambda: members.remove_member(
            db, campaign.id, actor_id=profile.id, target_id=target_id,
            expected_revision=expected_revision, operation_id=operation_id or idempotency_key,
        ),
    )


@router.get("/api/campaigns/{campaign_id}/characters")
def list_campaign_characters(
    campaign: Campaign = Depends(campaign_for("participant", forbidden="Not a member")),
    db: Session = Depends(get_db),
):
    """Public launch roster — only approved public character info, no secret lore."""
    return {"characters": members.launch_roster(db, campaign.id)}
