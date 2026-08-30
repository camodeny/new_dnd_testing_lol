"""Characters transport."""
import logging
import uuid as uuid_lib

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.characters.service import character_with_sheet
from app.deps.auth import resolve_profile
from database import get_db
from models import Character, Dnd5eCharacterSheet
from models import CharacterChatMessage as DbChatMessage

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/characters")
def list_characters(request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    chars = db.execute(select(Character).where(Character.owner_id == profile.id).order_by(Character.updated_at.desc())).scalars().all()
    result = []
    for c in chars:
        result.append(character_with_sheet(db, c))
    return {"characters": result}


@router.post("/api/characters")
def create_character(request: Request, payload: dict, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    name = payload.get("name") or payload.get("character_name") or "Unnamed Hero"
    char = Character(owner_id=profile.id, name=name, system=payload.get("system") or "dnd5e")
    db.add(char)
    db.flush()
    try:
        sheet = Dnd5eCharacterSheet.from_frontend(payload, owner_id=profile.id)
        sheet.character_id = char.id
        if not sheet.character_name:
            sheet.character_name = name
        db.add(sheet)
        db.commit()
        db.refresh(char)
        try:
            from sqlalchemy import update as sa_update

            db.execute(
                sa_update(DbChatMessage)
                .where(DbChatMessage.owner_id == profile.id, DbChatMessage.character_id.is_(None))
                .values(character_id=char.id)
            )
            db.commit()
        except Exception as e:
            logger.warning("failed to transfer draft chat: %s", e)
        sheet = db.execute(select(Dnd5eCharacterSheet).where(Dnd5eCharacterSheet.character_id == char.id)).scalars().first()
    except Exception as e:
        db.rollback()
        logger.exception("create character failed")
        raise HTTPException(status_code=400, detail=str(e))
    return {"character": character_with_sheet(db, char)}


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
    db.delete(char)
    db.commit()
    return {"ok": True}

