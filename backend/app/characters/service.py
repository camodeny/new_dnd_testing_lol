"""Characters service — pure helpers, no FastAPI."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.characters import Character
from models.characters import Dnd5eCharacterSheet


def latest_sheet(db: Session, character_id) -> Dnd5eCharacterSheet | None:
    """The character's current D&D 5e sheet (most recently updated row)."""
    return db.execute(
        select(Dnd5eCharacterSheet)
        .where(Dnd5eCharacterSheet.character_id == character_id)
        .order_by(Dnd5eCharacterSheet.updated_at.desc())
    ).scalars().first()


def roster_levels(db: Session, campaign_id) -> list[int]:
    """Levels of the PCs on a campaign's active roster (one per member with a sheet)."""
    from models.campaigns import CampaignMember

    ids = db.execute(
        select(CampaignMember.selected_character_id).where(
            CampaignMember.campaign_id == campaign_id,
            CampaignMember.selected_character_id.is_not(None),
        )
    ).scalars().all()
    levels = []
    for character_id in ids:
        sheet = latest_sheet(db, character_id)
        if sheet is not None:
            levels.append(int(sheet.level or 1))
    return levels


def character_with_sheet(db: Session, char: Character):
    sheet = latest_sheet(db, char.id)
    data = char.to_dict()
    if sheet:
        sheet_data = sheet.to_dict()
        for k in ("id", "character_id", "owner_id", "created_at", "updated_at"):
            sheet_data.pop(k, None)
        data.update(sheet_data)
        data["sheet"] = sheet.to_dict()
        data["id"] = str(char.id)
    return data

