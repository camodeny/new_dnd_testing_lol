"""Characters service — pure helpers, no FastAPI."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Character, Dnd5eCharacterSheet


def character_with_sheet(db: Session, char: Character):
    sheet = db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == char.id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()
    data = char.to_dict()
    if sheet:
        sheet_data = sheet.to_dict()
        for k in ("id", "character_id", "owner_id", "created_at", "updated_at"):
            sheet_data.pop(k, None)
        data.update(sheet_data)
        data["sheet"] = sheet.to_dict()
        data["id"] = str(char.id)
    return data

