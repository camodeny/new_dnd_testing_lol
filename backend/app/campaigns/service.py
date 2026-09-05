"""Campaigns application/domain helpers — no FastAPI imports.

These helpers are importable without importing the router (circular-safe),
so new gameplay modules can reuse validation without pulling in transport.
"""
import random
import json
import secrets
import string as _string
import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.campaigns import CampaignMember

RANDOM_CAMPAIGN_NAMES = [
    "The Whispering Hollow", "Embers of the Forgotten Keep", "Tides of Shadowfen",
    "The Clockwork Sanctum", "Wolves of Winter's Edge", "The Sunken Archive",
    "Ashen Crown", "The Starless Citadel", "Echoes of the Barrowlands",
]
RANDOM_CAMPAIGN_DESCS = [
    "Ancient ruins stir as a forgotten power awakens beneath the earth.",
    "A coastal town hires brave souls to investigate lights beyond the fog.",
    "Rival factions race to claim a relic that could reshape the realm.",
    "Whispers from another plane bleed into the forests—something is watching.",
]

CAMPAIGN_STATUSES = frozenset({"lobby", "starting", "active", "archived"})
CAMPAIGN_TRANSITIONS = {
    "lobby": frozenset({"starting", "archived"}),
    "starting": frozenset({"lobby", "active", "archived"}),
    "active": frozenset({"archived"}),
    "archived": frozenset(),
}
DIFFICULTIES = frozenset({"easy", "medium", "hard", "deadly"})
LOOT_MODES = frozenset({
    "frequent_gamble", "rare_treasure", "generous", "scarce",
    "rare_quality", "frequent", "rare",
})


def generate_invite_code(length: int = 8) -> str:
    alphabet = _string.ascii_uppercase + _string.digits
    alphabet = alphabet.replace("0", "").replace("O", "").replace("1", "").replace("I", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_campaign_member(db: Session, campaign_id: uuid_lib.UUID, user_id: uuid_lib.UUID) -> bool:
    return db.get(CampaignMember, {"campaign_id": campaign_id, "user_id": user_id}) is not None


def parse_campaign_id(campaign_id: str) -> uuid_lib.UUID:
    # Raises HTTPException-like ValueError handling is done by router mapping.
    # Domain helper raises ValueError so caller can map to HTTP 404.
    return uuid_lib.UUID(str(campaign_id))


def random_brief(seed: str | None = None) -> dict:
    return {
        "name": random.choice(RANDOM_CAMPAIGN_NAMES),
        "description": random.choice(RANDOM_CAMPAIGN_DESCS),
        "random_seed": seed or generate_invite_code(6),
    }


def validate_campaign_name(name: str) -> str:
    stripped = (name or "").strip()
    if not stripped:
        raise ValueError("Campaign name is required")
    if len(stripped) > 128:
        raise ValueError("Campaign name must be 128 characters or fewer")
    return stripped


def validate_seed(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s and len(s) > 128:
        raise ValueError("Seed must be 128 characters or fewer")
    return s or None


def normalize_required_players(val) -> int:
    if isinstance(val, bool):
        raise ValueError("Required players must be an integer from 1 to 6")
    if isinstance(val, float):
        raise ValueError("Required players must be an integer from 1 to 6")
    # Reject non-integral numeric strings like "2.5" explicitly via strict integer parsing;
    # int("2.5") already raises, but we also guard string floats early for clarity.
    if isinstance(val, str):
        stripped = val.strip()
        if stripped == "":
            raise ValueError("Required players must be an integer from 1 to 6")
        # Allow optional leading +/- but require pure integer digits after
        test = stripped.lstrip("+-")
        if not test.isdigit():
            raise ValueError("Required players must be an integer from 1 to 6")
    try:
        n = int(val) if val is not None else 1
    except (TypeError, ValueError):
        raise ValueError("Required players must be an integer from 1 to 6")
    if n < 1 or n > 6:
        raise ValueError("Required players must be between 1 and 6")
    return n


def normalize_loot_mode(val) -> str:
    loot_mode = str(val or "frequent_gamble").strip().lower()
    if loot_mode not in LOOT_MODES:
        raise ValueError("Invalid loot mode")
    return loot_mode


def validate_difficulty(val) -> str:
    difficulty = str(val or "medium").strip().lower()
    if difficulty not in DIFFICULTIES:
        raise ValueError("Difficulty must be easy, medium, hard, or deadly")
    return difficulty


def validate_optional_text(val, *, field: str, max_length: int) -> str | None:
    if val is None:
        return None
    value = str(val).strip()
    if len(value) > max_length:
        raise ValueError(f"{field} must be {max_length} characters or fewer")
    return value or None


def validate_content_boundaries(val) -> dict:
    if val is None:
        return {}
    if not isinstance(val, dict):
        raise ValueError("Content boundaries must be a JSON object")
    if len(json.dumps(val, ensure_ascii=False, separators=(",", ":"))) > 16_384:
        raise ValueError("Content boundaries must be 16384 characters or fewer")
    if len(val) > 32:
        raise ValueError("Content boundaries must have at most 32 entries")
    for key, value in val.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Content boundaries keys must be non-empty strings")
        if len(key) > 128:
            raise ValueError("Content boundaries key must be 128 characters or fewer")
        if isinstance(value, str):
            if len(value) > 2000:
                raise ValueError("Content boundaries string values must be 2000 characters or fewer")
        elif isinstance(value, list):
            if len(value) > 64:
                raise ValueError("Content boundaries lists must have at most 64 entries")
            for entry in value:
                if not isinstance(entry, str):
                    raise ValueError("Content boundaries list entries must be strings")
                if len(entry) > 500:
                    raise ValueError("Content boundaries list entries must be 500 characters or fewer")
                if len(entry.strip()) == 0:
                    raise ValueError("Content boundaries list entries must be non-empty strings")
        elif isinstance(value, dict):
            # Nested objects not allowed — keep boundaries structurally flat for generation.
            raise ValueError("Content boundaries values must be strings or arrays of strings")
        elif value is not None:
            raise ValueError("Content boundaries values must be strings or arrays of strings")
    return val


def validate_lifecycle_transition(current: str, target) -> str:
    target_status = str(target or "").strip().lower()
    if target_status not in CAMPAIGN_STATUSES:
        raise ValueError("Status must be lobby, starting, active, or archived")
    if target_status not in CAMPAIGN_TRANSITIONS.get(current, frozenset()):
        raise ValueError(f"Campaign cannot transition from {current} to {target_status}")
    return target_status


def is_launch_locked(status: str) -> bool:
    """Launch character assignment is locked once the lobby closes."""
    return str(status or "").strip().lower() != "lobby"


def character_launch_validity(character, sheet) -> dict:
    """Authoritative character setup/progress — issue #241.

    Valid launch PC requires: non-empty name, non-empty race, and a class
    (scalar char_class or non-empty classes list). Returns
    {is_valid, missing, progress}.
    """
    missing: list[str] = []
    name = (getattr(character, "name", "") or "").strip() if character is not None else ""
    if character is None or not name:
        missing.append("name")
    race = (getattr(sheet, "race", None) or "").strip() if sheet is not None else ""
    if not race:
        missing.append("race")
    char_class = (getattr(sheet, "char_class", None) or "").strip() if sheet is not None else ""
    classes = getattr(sheet, "classes", None) if sheet is not None else None
    has_class = bool(char_class) or (
        isinstance(classes, list)
        and any(isinstance(c, dict) and str(c.get("class_name") or "").strip() for c in classes)
    )
    if not has_class:
        missing.append("class")
    total = 3
    completed = total - len(missing)
    return {
        "is_valid": not missing,
        "missing": missing,
        "progress": {
            "completed": completed,
            "total": total,
            "percent": int(completed * 100 / total),
        },
    }


def compute_start_eligibility(campaign, members: list, db: Session) -> dict:
    """Server-side start eligibility from authoritative lobby/character state."""
    from models.characters import Character, Dnd5eCharacterSheet

    blockers: list[str] = []
    required = int(getattr(campaign, "required_players", 1) or 1)
    if len(members) < required:
        blockers.append(f"Campaign requires {required} members before starting (have {len(members)})")
    for m in members:
        label = f"Member {m.user_id}"
        char_id = getattr(m, "selected_character_id", None)
        if char_id is None:
            blockers.append(f"{label} has no selected character")
            continue
        char = db.get(Character, char_id)
        if char is None:
            blockers.append(f"{label} selected character is missing")
            continue
        if char.owner_id != m.user_id:
            blockers.append(f"{label} selected character is not owned by the member")
            continue
        sheet = db.execute(
            select(Dnd5eCharacterSheet)
            .where(Dnd5eCharacterSheet.character_id == char.id)
            .order_by(Dnd5eCharacterSheet.updated_at.desc())
        ).scalars().first()
        validity = character_launch_validity(char, sheet)
        if not validity["is_valid"]:
            blockers.append(
                f"{label} character incomplete: missing {', '.join(validity['missing'])}"
            )
        if not getattr(m, "is_ready", False):
            blockers.append(f"{label} is not ready")
    return {"eligible": not blockers, "blockers": blockers}
