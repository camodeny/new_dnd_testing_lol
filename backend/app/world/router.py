"""World transport — issue #209.

Authoritative current-scene + canonical world entities. All fictional
writers require expected_revision (optimistic concurrency → 409 on stale)
and an idempotency key (header or operation_id). Membership enforced:
owner or campaign member.
"""
from __future__ import annotations

import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.campaigns.events import RevisionConflictError
from app.campaigns.service import is_campaign_member, parse_campaign_id
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from app.world.service import (
    UNSET,
    create_entity_authoritative,
    entity_visible_to_viewer,
    filter_entities_for_viewer,
    get_current_scene,
    get_entity_strict,
    is_world_authority,
    list_entities,
    scene_visible_to_viewer,
    set_scene_authoritative,
    validate_entity_name,
    validate_entity_type,
)
from database import get_db
from models.campaigns import Campaign

router = APIRouter()
logger = logging.getLogger(__name__)


def _campaign_or_403(db: Session, cid: uuid_lib.UUID, profile) -> Campaign:
    camp = db.get(Campaign, cid)
    if not camp:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if camp.owner_id != profile.id and not is_campaign_member(db, camp.id, profile.id):
        raise HTTPException(status_code=403, detail="Not a member of this campaign")
    return camp


def _parse_campaign(campaign_id: str) -> uuid_lib.UUID:
    try:
        return parse_campaign_id(campaign_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid campaign id")


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


# ── Current scene ───────────────────────────────────────────────────────────

@router.get("/api/campaigns/{campaign_id}/world/current-scene")
def api_get_current_scene(campaign_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    cid = _parse_campaign(campaign_id)
    camp = _campaign_or_403(db, cid, profile)
    scene = get_current_scene(db, camp.id)
    # Viewer-aware: ordinary members never receive a restricted scene
    # verbatim — hide restricted existence as 404 (private-thread pattern).
    if scene is not None and not scene_visible_to_viewer(
        scene, is_world_authority(camp, profile.id)
    ):
        raise HTTPException(status_code=404, detail="Scene not found")
    return {"scene": scene.to_dict() if scene else None, "revision": camp.revision}


@router.put("/api/campaigns/{campaign_id}/world/current-scene")
def api_set_current_scene(
    campaign_id: str, payload: dict, request: Request, response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    cid = _parse_campaign(campaign_id)
    camp = _campaign_or_403(db, cid, profile)
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can update the current scene")
    expected = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    kwargs = {
        # Key-presence (not .get()): explicit null clears the canonical
        # reference while omission preserves it.
        "location_entity_id": payload["location_entity_id"] if "location_entity_id" in payload else UNSET,
        "location_name": payload.get("location_name"),
        "fictional_time": payload.get("fictional_time"),
        "fictional_time_details": payload.get("fictional_time_details"),
        "present_actors": payload.get("present_actors"),
        "environment": payload.get("environment"),
        "visibility": payload.get("visibility"),
    }

    def _execute():
        try:
            scene, event = set_scene_authoritative(
                db, cid, expected, **kwargs,
                operation_id=operation_id or idempotency_key, actor_id=profile.id,
            )
        except RevisionConflictError as exc:
            raise HTTPException(
                status_code=409, detail=str(exc),
                headers={"X-Current-Revision": str(exc.actual_revision)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"scene": scene.to_dict(), "event": event.to_dict(), "revision": scene.revision}

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="world.scene_updated", scope_type="campaign", scope_id=cid,
            payload=payload, execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


# ── Entities ────────────────────────────────────────────────────────────────

@router.get("/api/campaigns/{campaign_id}/world/entities")
def api_list_entities(
    campaign_id: str, request: Request, db: Session = Depends(get_db),
    entity_type: str | None = None, status: str | None = None,
    search: str | None = None, limit: int = 100,
):
    profile = resolve_profile(request, db)
    cid = _parse_campaign(campaign_id)
    camp = _campaign_or_403(db, cid, profile)
    try:
        entities = list_entities(
            db, camp.id, entity_type=entity_type, status=status, search=search, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Viewer-aware: restricted entities are filtered for ordinary members.
    entities = filter_entities_for_viewer(
        entities, is_world_authority(camp, profile.id)
    )
    return {"entities": [e.to_dict() for e in entities], "revision": camp.revision}


@router.post("/api/campaigns/{campaign_id}/world/entities")
def api_create_entity(
    campaign_id: str, payload: dict, request: Request, response: Response,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    cid = _parse_campaign(campaign_id)
    camp = _campaign_or_403(db, cid, profile)
    if camp.owner_id != profile.id:
        raise HTTPException(status_code=403, detail="Only the owner can create world entities")
    expected = _expected_revision(payload)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id or payload.get("idempotency_key"))
    try:
        entity_type = validate_entity_type(payload.get("entity_type"))
        name = validate_entity_name(payload.get("name"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    def _execute():
        try:
            entity, event = create_entity_authoritative(
                db, cid, expected,
                entity_type=entity_type, name=name,
                summary=payload.get("summary"),
                status=payload.get("status") or "active",
                visibility=payload.get("visibility") or "campaign",
                details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
                operation_id=operation_id or idempotency_key,
                actor_id=profile.id,
                idempotency_key=str(payload.get("idempotency_key") or operation_id or idempotency_key),
            )
        except RevisionConflictError as exc:
            raise HTTPException(
                status_code=409, detail=str(exc),
                headers={"X-Current-Revision": str(exc.actual_revision)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "entity": entity.to_dict(),
            "event": event.to_dict() if event else None,
            "duplicate": event is None,
        }

    try:
        return execute_http_idempotent(
            db, response, actor_id=profile.id, idempotency_key=idempotency_key,
            command_type="world.entity_created", scope_type="campaign", scope_id=cid,
            payload=payload, execute=_execute,
        )
    except RevisionConflictError as exc:
        raise HTTPException(
            status_code=409, detail=str(exc),
            headers={"X-Current-Revision": str(exc.actual_revision)},
        ) from exc


@router.get("/api/campaigns/{campaign_id}/world/entities/{entity_id}")
def api_get_entity(campaign_id: str, entity_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    cid = _parse_campaign(campaign_id)
    camp = _campaign_or_403(db, cid, profile)
    try:
        eid = uuid_lib.UUID(str(entity_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid entity id")
    try:
        entity = get_entity_strict(db, cid, eid)
    except ValueError:
        raise HTTPException(status_code=404, detail="World entity not found")
    # Viewer-aware: hide restricted entities from ordinary members as 404.
    if not entity_visible_to_viewer(entity, is_world_authority(camp, profile.id)):
        raise HTTPException(status_code=404, detail="World entity not found")
    return {"entity": entity.to_dict()}
