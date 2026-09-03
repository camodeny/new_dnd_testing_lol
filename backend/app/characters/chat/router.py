"""Character chat transport."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.characters.chat.service import (
    CharacterChatRequest,
    character_chat_sync_generator,
    resolve_character_uuid,
    save_chat_message,
)
from app.deps.auth import resolve_profile
from database import get_db
from models.characters import Character
from models.characters import CharacterChatMessage

router = APIRouter()


def _resolve_or_404(character_id: str):
    try:
        return resolve_character_uuid(character_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Invalid character id")


@router.get("/api/characters/{character_id}/chat")
def get_character_chat(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    character_uuid = _resolve_or_404(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")
    msgs = (
        db.execute(
            select(CharacterChatMessage)
            .where(
                CharacterChatMessage.owner_id == profile.id,
                CharacterChatMessage.character_id == character_uuid,
            )
            .order_by(CharacterChatMessage.created_at.asc())
        )
        .scalars()
        .all()
    )
    return {
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in msgs
        ]
    }


@router.delete("/api/characters/{character_id}/chat")
def delete_character_chat(character_id: str, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    character_uuid = _resolve_or_404(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")
    db.execute(
        sa_delete(CharacterChatMessage).where(
            CharacterChatMessage.owner_id == profile.id, CharacterChatMessage.character_id == character_uuid
        )
    )
    db.commit()
    return {"ok": True}


@router.post("/api/characters/{character_id}/chat")
async def character_chat(character_id: str, req: CharacterChatRequest, request: Request, db: Session = Depends(get_db)):
    profile = resolve_profile(request, db)
    character_uuid = _resolve_or_404(character_id)
    if character_uuid is not None:
        char = db.get(Character, character_uuid)
        if not char or char.owner_id != profile.id:
            raise HTTPException(status_code=404, detail="Character not found")
    save_chat_message(profile.id, character_uuid, "user", req.content)
    return StreamingResponse(
        character_chat_sync_generator(req, profile.id, character_uuid),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

