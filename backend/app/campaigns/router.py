"""Campaigns transport — APIRouter. Depends on service layer for domain logic."""
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.campaigns.events import (
    RevisionConflictError,
    commit_campaign_mutation,
    list_campaign_events,
)
from app.campaigns.service import (
    generate_invite_code,
    is_campaign_member,
    normalize_loot_mode,
    normalize_required_players,
    parse_campaign_id,
    random_brief,
    validate_campaign_name,
    validate_seed,
)
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models import Campaign, CampaignInvite, CampaignMember, Profile

router = APIRouter()


@router.get("/api/campaigns")
def list_campaigns(request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    member_rows = db.execute(select(CampaignMember.campaign_id).where(CampaignMember.user_id == profile.id)).scalars().all()
    member_ids = set(member_rows)
    rows = db.execute(select(Campaign).order_by(Campaign.updated_at.desc())).scalars().all()
    visible = [c for c in rows if c.owner_id == profile.id or c.id in member_ids]
    return {"campaigns": [c.to_dict() for c in visible]}


@router.post("/api/campaigns")
def create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        name = validate_campaign_name(payload.get("name") or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    description = payload.get("description")
    try:
        random_seed = validate_seed(payload.get("random_seed") or payload.get("seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    required_players = normalize_required_players(payload.get("required_players") or payload.get("requiredPlayers"))
    loot_mode = normalize_loot_mode(payload.get("loot_mode") or payload.get("lootMode"))
    camp = Campaign(
        owner_id=profile.id,
        name=name,
        description=description,
        random_seed=random_seed,
        required_players=required_players,
        loot_mode=loot_mode,
    )
    db.add(camp)
    db.flush()
    member = CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner")
    db.add(member)
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict()}


@router.post("/api/campaigns/random-brief")
def random_campaign_brief(payload: dict, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    seed = payload.get("random_seed") or payload.get("seed") or None
    brief = random_brief(seed if isinstance(seed, str) else None)
    brief["seed"] = brief["random_seed"]
    return brief


@router.post("/api/campaigns/quick-create")
def quick_create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    brief = random_brief()
    required_players = normalize_required_players(payload.get("required_players"))
    # Preserve previous behavior: keep any supplied string verbatim, only default
    # when missing. Normalization of unknown values to frequent_gamble is only
    # for the main create path, not quick-create (behavior-preserving refactor).
    loot_mode = str(payload.get("loot_mode") or "frequent_gamble")
    camp = Campaign(
        owner_id=profile.id,
        name=brief["name"],
        description=brief["description"],
        random_seed=brief["random_seed"],
        required_players=required_players,
        loot_mode=loot_mode,
    )
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner"))
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict(), "brief": brief}


@router.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, camp.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return {"campaign": camp.to_dict()}


@router.delete("/api/campaigns/{campaign_id}")
def delete_campaign(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can delete this campaign")
    if list_campaign_events(db, cid, limit=1):
        raise HTTPException(status_code=409, detail="Campaigns with domain-event history cannot be deleted")
    db.delete(camp)
    db.commit()
    return {"ok": True}


@router.put("/api/campaigns/{campaign_id}")
def update_campaign(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can update")
    changes = {}
    if "name" in payload and payload["name"] is not None:
        try:
            changes["name"] = validate_campaign_name(str(payload["name"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "description" in payload:
        changes["description"] = payload["description"]
    if "random_seed" in payload or "seed" in payload:
        try:
            changes["random_seed"] = validate_seed(payload.get("random_seed") or payload.get("seed") or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected_revision = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(campaign: Campaign):
        for field, value in changes.items():
            setattr(campaign, field, value)

    def _execute():
        campaign, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.settings_updated",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            payload={"changes": changes},
            mutate=_mutate,
            commit=False,
        )
        return {"campaign": campaign.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.settings_updated",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e),
            headers={"X-Current-Revision": str(e.actual_revision)},
        )


@router.post("/api/campaigns/{campaign_id}/mutations")
def commit_campaign_mutation_endpoint(
    campaign_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can mutate this campaign")
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        expected = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type is required")
    operation_id = payload.get("operation_id") or payload.get("operationId")
    if operation_id is not None:
        operation_id = str(operation_id).strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)
    visibility = str(payload.get("visibility") or "public").strip() or "public"
    provenance = payload.get("provenance")
    targets = payload.get("targets")
    event_payload = payload.get("payload")
    mutate_fields = payload.get("mutate") if isinstance(payload.get("mutate"), dict) else None

    def _mutate(campaign: Campaign):
        if not mutate_fields:
            return
        if "name" in mutate_fields and mutate_fields["name"] is not None:
            try:
                campaign.name = validate_campaign_name(str(mutate_fields["name"]))
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if "description" in mutate_fields:
            campaign.description = mutate_fields["description"]

    def _execute():
        campaign, event = commit_campaign_mutation(
            db,
            cid,
            expected,
            event_type=event_type,
            payload=event_payload if isinstance(event_payload, dict) else ({"data": event_payload} if event_payload is not None else None),
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets=targets,
            visibility=visibility,
            provenance=provenance if isinstance(provenance, dict) else None,
            mutate=_mutate if mutate_fields else None,
            commit=False,
        )
        return {"campaign": campaign.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.mutation",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as e:
        raise HTTPException(
            status_code=409,
            detail=str(e),
            headers={"X-Current-Revision": str(e.actual_revision), "X-Expected-Revision": str(e.expected_revision)},
        )


@router.get("/api/campaigns/{campaign_id}/events")
def list_campaign_domain_events(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    events = list_campaign_events(db, cid, viewer_id=profile.id, limit=200)
    return {"events": [event.to_dict() for event in events], "revision": camp.revision}


@router.get("/api/campaigns/{campaign_id}/members")
def list_campaign_members(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, cid, profile.id):
        raise HTTPException(status_code=403, detail="Not a member")
    members = db.execute(select(CampaignMember).where(CampaignMember.campaign_id == cid)).scalars().all()
    out = []
    for m in members:
        prof = db.get(Profile, m.user_id)
        out.append({
            "user_id": str(m.user_id),
            "username": prof.username if prof else "adventurer",
            "email": prof.email if prof else None,
            "role": m.role,
        })
    return {"members": out}


@router.get("/api/campaigns/{campaign_id}/characters")
def list_campaign_characters(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    return {"characters": []}


@router.get("/api/campaigns/{campaign_id}/invites")
def get_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can view invite")
    inv = db.get(CampaignInvite, cid)
    if not inv:
        return {"code": None}
    return {"code": inv.code}


@router.post("/api/campaigns/{campaign_id}/invites")
def create_campaign_invite(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can create invite")
    existing = db.get(CampaignInvite, cid)
    if existing:
        return {"code": existing.code}
    for _ in range(5):
        code = generate_invite_code()
        if not db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first():
            inv = CampaignInvite(campaign_id=cid, code=code)
            db.add(inv)
            db.commit()
            return {"code": code}
    raise HTTPException(status_code=500, detail="Failed to generate invite")


@router.get("/api/invites/lookup")
def lookup_invite(code: str, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    clean = code.strip().upper()
    if not clean:
        raise HTTPException(status_code=400, detail="Code required")
    inv = db.execute(select(CampaignInvite).where(CampaignInvite.code == clean)).scalars().first()
    if not inv:
        raise HTTPException(status_code=404, detail="Invite not found")
    camp = db.get(Campaign, inv.campaign_id)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return {"campaign_id": str(camp.id), "campaign": camp.to_dict()}


@router.post("/api/campaigns/{campaign_id}/join")
def join_campaign(campaign_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    code = str(payload.get("code") or "").strip().upper()
    if not code:
        raise HTTPException(status_code=400, detail="Invite code required")
    inv = db.get(CampaignInvite, cid)
    if not inv or inv.code != code:
        inv2 = db.execute(select(CampaignInvite).where(CampaignInvite.code == code)).scalars().first()
        if not inv2 or inv2.campaign_id != cid:
            raise HTTPException(status_code=403, detail="Invalid invite code")
    if not is_campaign_member(db, cid, profile.id):
        db.add(CampaignMember(campaign_id=cid, user_id=profile.id, role="player"))
        db.commit()
    return {"ok": True, "campaign": camp.to_dict()}
