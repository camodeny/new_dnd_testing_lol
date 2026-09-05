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


@router.get("/api/characters")
def list_characters(request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    chars = db.execute(select(Character).where(Character.owner_id == profile.id).order_by(Character.updated_at.desc())).scalars().all()
    result = []
    for c in chars:
        result.append(character_with_sheet(db, c))
    return {"characters": result}


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
    new_name = payload.get("name") or payload.get("character_name")
    if new_name:
        char.name = new_name
    sheet = db.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char.id)).scalars().first()
    if sheet:
        try:
            updated = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
            for col in Dnd5eCharacterSheet.__table__.columns:
                key = col.name
                if key in ("id", "character_id", "owner_id", "created_at"):
                    continue
                val = getattr(updated, key, None)
                if val is not None:
                    setattr(sheet, key, val)
            if new_name:
                sheet.character_name = new_name
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    else:
        sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
        sheet.character_id = char.id
        db.add(sheet)
    db.commit()
    db.refresh(char)
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
