"""Campaigns application/domain helpers — no FastAPI imports.

These helpers are importable without importing the router (circular-safe),
so new gameplay modules can reuse validation without pulling in transport.
"""
import random
import secrets
import string as _string
import uuid as uuid_lib

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Campaign, CampaignMember

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
    try:
        n = int(val) if val is not None else 1
    except Exception:
        n = 1
    return max(1, min(8, n))


def normalize_loot_mode(val) -> str:
    loot_mode = str(val or "frequent_gamble")
    if loot_mode not in ("frequent_gamble", "rare_treasure", "generous", "scarce", "rare_quality", "frequent", "rare"):
        return "frequent_gamble"
    return loot_mode

