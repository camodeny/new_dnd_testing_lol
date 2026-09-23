"""Characters transport."""
import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select, update as sa_update
from sqlalchemy.orm import Session

from app.characters.service import character_with_sheet
from app.deps.auth import resolve_profile
from app.deps.idempotency import execute_http_idempotent, require_idempotency_key
from database import get_db
from models.characters import Character
from models.characters import Dnd5eCharacterSheet
from models.characters import CharacterChatMessage as DbChatMessage

logger = logging.getLogger(__name__)

router = APIRouter()


def _launch_locking_campaign(db: Session, character_id) -> str | None:
    """Return campaign id if this character is a locked launch PC (status != lobby)."""
    from models.campaigns import Campaign, CampaignMember

    rows = db.execute(
        select(Campaign)
        .join(CampaignMember, Campaign.id == CampaignMember.campaign_id)
        .where(CampaignMember.selected_character_id == character_id)
        .order_by(Campaign.id)
        .with_for_update(of=Campaign)
        .execution_options(populate_existing=True)
    ).scalars().all()
    for camp in rows:
        if str(getattr(camp, "status", "lobby")) != "lobby":
            return str(camp.id)
    return None


def _update_sheet(db: Session, char: Character, owner_id, payload: dict):
    existing = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == char.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()
    updated = Dnd5eCharacterSheet.from_frontend(payload, owner_id=owner_id)
    updated.character_id = char.id
    if existing:
        for col in Dnd5eCharacterSheet.__table__.columns:
            key = col.name
            if key in ("id", "character_id", "owner_id", "created_at"):
                continue
            value = getattr(updated, key, None)
            if value is not None:
                setattr(existing, key, value)
    else:
        db.add(updated)
    return updated


@router.get("/api/characters")
def list_characters(request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    chars = db.execute(select(Character).where(Character.owner_id == profile.id).order_by(Character.updated_at.desc())).scalars().all()
    result = []
    for c in chars:
        result.append(character_with_sheet(db, c))
    return {"characters": result}


@router.post("/api/characters/drafts", status_code=201)
def create_character_draft(
    request: Request,
    response: Response,
    payload: dict,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        char = Character(
            owner_id=profile.id,
            system="dnd5e",
            name="Untitled Character",
            status="draft",
            creator_step="identity",
        )
        db.add(char)
        db.flush()
        sheet = Dnd5eCharacterSheet.from_frontend(
            {"name": "Untitled Character"}, owner_id=profile.id,
        )
        sheet.character_id = char.id
        db.add(sheet)
        db.flush()
        return {"character": character_with_sheet(db, char)}

    return execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="character.draft.create",
        scope_type="user",
        scope_id=profile.id,
        payload=payload,
        execute=_execute,
    )


@router.post("/api/characters")
def create_character(
    request: Request,
    response: Response,
    payload: dict,
    db: Session = Depends(get_db),
):
    profile = resolve_profile(request, db)
    operation_id = str(payload.get("operation_id") or "").strip() or None
    idempotency_key = require_idempotency_key(request, operation_id)

    def _execute():
        try:
            name = payload.get("name") or payload.get("character_name") or "Unnamed Hero"
            char = Character(owner_id=profile.id, name=name, system=payload.get("system") or "dnd5e")
            db.add(char)
            db.flush()
            sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
            sheet.character_id = char.id
            if not sheet.character_name:
                sheet.character_name = name
            db.add(sheet)
            db.execute(
                sa_update(DbChatMessage)
                .where(DbChatMessage.owner_id == profile.id, DbChatMessage.character_id.is_(None))
                .values(character_id=char.id)
            )
            db.flush()
            return {"character": character_with_sheet(db, char)}
        except Exception as exc:
            logger.exception("create character failed")
            raise ValueError(str(exc)) from exc

    return execute_http_idempotent(
        db,
        response,
        actor_id=profile.id,
        idempotency_key=idempotency_key,
        command_type="character.create",
        scope_type="user",
        scope_id=profile.id,
        payload=payload,
        execute=_execute,
    )


@router.get("/api/characters/{character_id}")
def get_character(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    return {"character": character_with_sheet(db, char)}


@router.put("/api/characters/{character_id}")
def update_character(character_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    locked_campaign = _launch_locking_campaign(db, char.id)
    if locked_campaign:
        logger.warning(
            "character edit rejected character_id=%s actor_id=%s campaign_id=%s reason=launch_locked",
            char.id, profile.id, locked_campaign,
        )
        raise HTTPException(
            status_code=409,
            detail="Launch character is locked after campaign start; progression only",
        )
    from app.campaigns.replacements import is_historical_canon as _is_canon

    if _is_canon(db, char.id):
        logger.warning(
            "character edit rejected character_id=%s actor_id=%s reason=historical_canon",
            char.id, profile.id,
        )
        raise HTTPException(
            status_code=409,
            detail="Fallen PCs are preserved as historical canon and cannot be edited",
        )
    new_name = str(payload.get("name") or payload.get("character_name") or "").strip()
    try:
        updated_sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
        if char.status == "draft":
            from types import SimpleNamespace

            from app.campaigns.service import character_launch_validity

            validity = character_launch_validity(
                SimpleNamespace(name=new_name, status="complete"), updated_sheet,
            )
            if not validity["is_valid"]:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": f"Complete the required character fields: {', '.join(validity['missing'])}",
                        "missing": validity["missing"],
                    },
                )
        _update_sheet(db, char, profile.id, payload)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if new_name:
        char.name = new_name
    if char.status == "draft":
        char.status = "complete"
        char.creator_step = None
    db.commit()
    db.refresh(char)
    return {"character": character_with_sheet(db, char)}


@router.put("/api/characters/{character_id}/draft")
def update_character_draft(character_id: str, payload: dict, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Draft not found")
    if char.status != "draft":
        raise HTTPException(status_code=409, detail="Character is no longer a draft")
    creator_step = payload.get("creator_step", char.creator_step or "identity")
    allowed_steps = {"identity", "scores", "combat", "magic_gear", "story"}
    if creator_step not in allowed_steps:
        raise HTTPException(status_code=422, detail="Invalid character creator step")
    new_name = str(payload.get("name") or payload.get("character_name") or "").strip()
    try:
        _update_sheet(db, char, profile.id, payload)
        char.name = new_name or "Untitled Character"
        char.creator_step = creator_step
        db.commit()
        db.refresh(char)
    except Exception as exc:
        db.rollback()
        logger.exception("update character draft failed character_id=%s", cid)
        raise HTTPException(status_code=400, detail="Could not save character draft") from exc
    return {"character": character_with_sheet(db, char)}


@router.delete("/api/characters/{character_id}")
def delete_character(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    try:
        cid = uuid_lib.UUID(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")
    char = db.get(Character, cid)
    if not char or char.owner_id != profile.id:
        raise HTTPException(status_code=404, detail="Character not found")
    locked_campaign = _launch_locking_campaign(db, char.id)
    if locked_campaign:
        logger.warning(
            "character delete rejected character_id=%s actor_id=%s campaign_id=%s reason=launch_locked",
            char.id, profile.id, locked_campaign,
        )
        raise HTTPException(
            status_code=409,
            detail="Launch character is locked after campaign start; progression only",
        )
    from app.campaigns.replacements import is_historical_canon as _is_canon

    if _is_canon(db, char.id):
        logger.warning(
            "character delete rejected character_id=%s actor_id=%s reason=historical_canon",
            char.id, profile.id,
        )
        raise HTTPException(
            status_code=409,
            detail="Fallen PCs are preserved as historical canon and cannot be deleted",
        )
    from models.campaigns import CampaignMember

    lobby_selections = db.execute(
        select(CampaignMember).where(CampaignMember.selected_character_id == char.id)
    ).scalars().all()
    for m in lobby_selections:
        m.selected_character_id = None
        m.is_ready = False
        m.ready_at = None
    db.delete(char)
    db.commit()
    return {"ok": True}
