"""Campaigns transport — APIRouter. Depends on service layer for domain logic."""
import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, func, select
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
    validate_content_boundaries,
    validate_difficulty,
    validate_lifecycle_transition,
    validate_optional_text,
    validate_seed,
)
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models import (
    Campaign,
    CampaignInvite,
    CampaignMember,
    CampaignThread,
    CampaignThreadMember,
    Profile,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _expected_revision(payload: dict) -> int:
    if "expected_revision" not in payload:
        raise HTTPException(status_code=400, detail="expected_revision is required")
    try:
        revision = int(payload["expected_revision"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="expected_revision must be an integer")
    if revision < 0:
        raise HTTPException(status_code=400, detail="expected_revision must be a non-negative integer")
    return revision


def _validated_setup(payload: dict, *, creation: bool = False) -> dict:
    changes = {}
    try:
        if creation or "required_players" in payload:
            raw = payload.get("required_players")
            changes["required_players"] = normalize_required_players(raw)
        if creation or "loot_mode" in payload:
            raw = payload.get("loot_mode")
            changes["loot_mode"] = normalize_loot_mode(raw)
        if creation or "difficulty" in payload:
            changes["difficulty"] = validate_difficulty(payload.get("difficulty"))
        if "theme" in payload:
            changes["theme"] = validate_optional_text(payload.get("theme"), field="Theme", max_length=128)
        if "brief" in payload:
            changes["brief"] = validate_optional_text(payload.get("brief"), field="Brief", max_length=4000)
        if creation or "content_boundaries" in payload:
            changes["content_boundaries"] = validate_content_boundaries(payload.get("content_boundaries"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return changes


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
        random_seed = validate_seed(payload.get("random_seed"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    setup = _validated_setup(payload, creation=True)
    camp = Campaign(
        owner_id=profile.id,
        name=name,
        description=description,
        random_seed=random_seed,
        **setup,
    )
    db.add(camp)
    db.flush()
    member = CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner")
    db.add(member)
    db.flush()
    # Create durable shared thread on campaign creation — snapshot GET is retrieval-only (#196)
    from models import CampaignThread

    db.add(
        CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="campaign",
            title="Campaign",
            created_by=profile.id,
        )
    )
    db.commit()
    db.refresh(camp)
    return {"campaign": camp.to_dict()}


@router.post("/api/campaigns/random-brief")
def random_campaign_brief(payload: dict, request: Request, db: Session = Depends(get_db)):
    resolve_profile(request, db)
    seed = payload.get("random_seed") or None
    brief = random_brief(seed if isinstance(seed, str) else None)
    return brief


@router.post("/api/campaigns/quick-create")
def quick_create_campaign(payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    brief = random_brief()
    setup = _validated_setup(payload, creation=True)
    camp = Campaign(
        owner_id=profile.id,
        name=brief["name"],
        description=brief["description"],
        random_seed=brief["random_seed"],
        **setup,
    )
    db.add(camp)
    db.flush()
    db.add(CampaignMember(campaign_id=camp.id, user_id=profile.id, role="owner"))
    db.flush()
    from models import CampaignThread as _CampaignThread

    db.add(
        _CampaignThread(
            id=uuid_lib.uuid4(),
            campaign_id=camp.id,
            thread_type="campaign",
            title="Campaign",
            created_by=profile.id,
        )
    )
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
    changes = _validated_setup(payload)
    if "name" in payload and payload["name"] is not None:
        try:
            changes["name"] = validate_campaign_name(str(payload["name"]))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if "description" in payload:
        changes["description"] = payload["description"]
    if "random_seed" in payload:
        try:
            changes["random_seed"] = validate_seed(payload.get("random_seed") or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    expected_revision = _expected_revision(payload)
    if not changes:
        raise HTTPException(status_code=400, detail="At least one campaign setting is required")
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(campaign: Campaign):
        if campaign.status != "lobby":
            logger.warning(
                "campaign settings lock rejection campaign_id=%s actor_id=%s status=%s",
                campaign.id, profile.id, campaign.status,
            )
            raise HTTPException(status_code=409, detail="Campaign settings are locked after the lobby")
        if "required_players" in changes:
            member_count = db.scalar(
                select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == campaign.id)
            ) or 0
            if changes["required_players"] < member_count:
                raise HTTPException(
                    status_code=409,
                    detail="Required players cannot be lower than current membership; remove members first",
                )
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


@router.post("/api/campaigns/{campaign_id}/lifecycle")
def transition_campaign_lifecycle(
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
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can change campaign lifecycle")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)
    target_raw = payload.get("status")

    def _mutate(locked: Campaign):
        try:
            target = validate_lifecycle_transition(locked.status, target_raw)
        except ValueError as exc:
            logger.warning(
                "campaign lifecycle transition rejected campaign_id=%s actor_id=%s from=%s to=%s",
                locked.id, profile.id, locked.status, target_raw,
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if target == "starting":
            member_count = db.scalar(
                select(func.count()).select_from(CampaignMember).where(CampaignMember.campaign_id == locked.id)
            ) or 0
            if member_count < locked.required_players:
                raise HTTPException(
                    status_code=409,
                    detail=f"Campaign requires {locked.required_players} members before starting",
                )
        locked.status = target

    def _execute():
        current = db.get(Campaign, cid)
        target = str(target_raw or "").strip().lower()
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type=f"campaign.lifecycle.{target or 'invalid'}",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            payload={"from": current.status if current else None, "to": target},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign lifecycle transitioned campaign_id=%s actor_id=%s status=%s revision=%s",
            cid, profile.id, campaign_after.status, campaign_after.revision,
        )
        return {"campaign": campaign_after.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.lifecycle.transition",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


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
    operation_id = payload.get("operation_id")
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
        if campaign.status != "lobby":
            raise HTTPException(status_code=409, detail="Campaign settings are locked after the lobby")
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


@router.delete("/api/campaigns/{campaign_id}/members/{user_id}")
def remove_campaign_member(
    campaign_id: str,
    user_id: str,
    payload: dict,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    try:
        cid = parse_campaign_id(campaign_id)
        target_id = uuid_lib.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign or user id")
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can remove campaign members")
    if target_id == campaign.owner_id:
        raise HTTPException(status_code=400, detail="Campaign owner cannot be removed")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            logger.warning(
                "campaign membership removal rejected campaign_id=%s actor_id=%s target_id=%s status=%s",
                cid, profile.id, target_id, locked.status,
            )
            raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
        member = db.get(CampaignMember, {"campaign_id": cid, "user_id": target_id})
        if member is None:
            raise HTTPException(status_code=404, detail="Campaign member not found")
        db.delete(member)
        campaign_thread_ids = select(CampaignThread.id).where(CampaignThread.campaign_id == cid)
        db.execute(
            delete(CampaignThreadMember).where(
                CampaignThreadMember.user_id == target_id,
                CampaignThreadMember.thread_id.in_(campaign_thread_ids),
            )
        )

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.member_removed",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            targets={"user_id": str(target_id)},
            payload={"user_id": str(target_id)},
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign member removed campaign_id=%s actor_id=%s target_id=%s revision=%s",
            cid, profile.id, target_id, campaign_after.revision,
        )
        return {"ok": True, "campaign": campaign_after.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.member.remove",
            scope_type="campaign",
            scope_id=cid,
            payload={**payload, "user_id": str(target_id)},
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


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
    if camp.status != "lobby":
        logger.warning(
            "campaign invite creation rejected campaign_id=%s actor_id=%s status=%s",
            cid, profile.id, camp.status,
        )
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
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


@router.delete("/api/campaigns/{campaign_id}/invites")
def revoke_campaign_invite(
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
    campaign = db.get(Campaign, cid)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if campaign.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only owner can revoke invite")
    expected_revision = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _mutate(locked: Campaign):
        if locked.status != "lobby":
            raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
        invite = db.get(CampaignInvite, cid)
        if invite is None:
            raise HTTPException(status_code=404, detail="Campaign invite not found")
        db.delete(invite)

    def _execute():
        campaign_after, event = commit_campaign_mutation(
            db,
            cid,
            expected_revision,
            event_type="campaign.invite_revoked",
            operation_id=operation_id or idempotency_key,
            actor_id=profile.id,
            mutate=_mutate,
            commit=False,
        )
        logger.info(
            "campaign invite revoked campaign_id=%s actor_id=%s revision=%s",
            cid, profile.id, campaign_after.revision,
        )
        return {"ok": True, "campaign": campaign_after.to_dict(), "event": event.to_dict()}

    try:
        return execute_http_idempotent(
            db,
            response,
            actor_id=profile.id,
            idempotency_key=idempotency_key,
            command_type="campaign.invite.revoke",
            scope_type="campaign",
            scope_id=cid,
            payload=payload,
            execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


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
    camp = db.execute(
        select(Campaign).where(Campaign.id == cid).with_for_update()
    ).scalars().first()
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
    if is_campaign_member(db, cid, profile.id):
        return {"ok": True, "campaign": camp.to_dict()}
    if camp.status != "lobby":
        db.rollback()
        logger.warning(
            "campaign join rejected by membership lock campaign_id=%s actor_id=%s status=%s",
            cid, profile.id, camp.status,
        )
        raise HTTPException(status_code=409, detail="Campaign membership is locked after the lobby")
    db.add(CampaignMember(campaign_id=cid, user_id=profile.id, role="player"))
    db.commit()
    logger.info("campaign member joined campaign_id=%s actor_id=%s", cid, profile.id)
    return {"ok": True, "campaign": camp.to_dict()}
